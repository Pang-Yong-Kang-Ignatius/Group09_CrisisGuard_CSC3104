"""Teardown inventory must remain read-only, scoped and explicit about limits."""
import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest

spec=importlib.util.spec_from_file_location('cleanup_launcher',Path(__file__).parents[2]/'infrastructure/aws/launch_lab.py')
launcher=importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


@pytest.fixture
def aws():
    account,region,stack='123456789012','us-east-1','team-lab'
    main_id=f'arn:aws:cloudformation:{region}:{account}:stack/{stack}/one'
    source_id=main_id.replace('/team-lab/','/team-lab-source/')
    states={name:{'StackId':arn,'StackStatus':'CREATE_COMPLETE','Tags':[{'Key':'Project','Value':stack}]}
            for name,arn in [(stack,main_id),(stack+'-source',source_id)]}
    resources=[{'LogicalResourceId':name,'ResourceType':'AWS::EC2::Instance','PhysicalResourceId':f'i-{i}'}
               for i,name in enumerate(('Cloud','HospitalA','HospitalB'))]
    resources += [{'LogicalResourceId':name,'ResourceType':'AWS::EC2::SecurityGroup','PhysicalResourceId':'sg-'+name}
                  for name in ('CloudSecurity','HospitalSecurity')]
    resources += [{'LogicalResourceId':f'Api{port}','ResourceType':'AWS::EC2::SecurityGroupIngress','PhysicalResourceId':f'rule-{port}'}
                  for port in (8001,8003,8004)]
    buckets=[{'LogicalResourceId':'Source','ResourceType':'AWS::S3::Bucket','PhysicalResourceId':'team-private-source'}]
    hosts=[{'InstanceId':f'i-{i}','State':{'Name':'stopped'},'Tags':[{'Key':'aws:cloudformation:stack-id','Value':main_id}],
            'BlockDeviceMappings':[{'DeviceName':'/dev/sda1','Ebs':{'VolumeId':f'vol-{i}','DeleteOnTermination':False}}]}
           for i in range(3)]
    volumes=[{'VolumeId':f'vol-{i}','Size':20 if i==0 else 16,'Encrypted':True,'State':'in-use',
              'Attachments':[{'InstanceId':f'i-{i}'}]} for i in range(3)]
    # Spec-limited clients cannot call create/delete/stop APIs.
    cf=Mock(spec=['describe_stacks','get_paginator'])
    cf.describe_stacks.side_effect=lambda StackName: {'Stacks':[states[StackName]]}
    paginator=Mock(spec=['paginate'])
    paginator.paginate.side_effect=lambda StackName: ([{'StackResourceSummaries':resources[:2]},
         {'StackResourceSummaries':resources[2:]}] if StackName==main_id else [{'StackResourceSummaries':buckets}])
    cf.get_paginator.return_value=paginator
    ec2=Mock(spec=['describe_instances','describe_volumes'])
    ec2.describe_instances.return_value={'Reservations':[{'Instances':hosts}]}
    ec2.describe_volumes.return_value={'Volumes':volumes}
    return locals()


def plan(aws):
    return launcher.cleanup_plan(aws['cf'],aws['ec2'],aws['stack'],aws['account'],aws['region'])


def test_paginated_inventory_includes_retained_disks_without_mutations(aws):
    report=plan(aws)
    assert report['changes_made'] is report['deletion_authorized'] is False
    assert len(report['instances'])==len(report['attached_volumes'])==3
    assert sum(v['size_gib'] for v in report['attached_volumes'])==52
    assert all(v['delete_on_termination'] is False for v in report['attached_volumes'])
    assert report['source_bucket']=='team-private-source'
    assert 'post-deletion' in ' '.join(report['limitations'])
    aws['cf'].get_paginator.assert_called_with('list_stack_resources')


@pytest.mark.parametrize('fault',['wrong_account','wrong_project','changing_stack','extra_resource',
    'missing_instance','foreign_instance','missing_disk','foreign_attachment','missing_volume_metadata'])
def test_ambiguous_ownership_or_inventory_fails_closed(aws,fault):
    if fault=='wrong_account': aws['account']='999999999999'
    elif fault=='wrong_project': aws['states'][aws['stack']]['Tags']=[]
    elif fault=='changing_stack': aws['states'][aws['stack']]['StackStatus']='UPDATE_IN_PROGRESS'
    elif fault=='extra_resource': aws['resources'].append({'LogicalResourceId':'Other','ResourceType':'AWS::S3::Bucket','PhysicalResourceId':'unrelated'})
    elif fault=='missing_instance': aws['hosts'].pop()
    elif fault=='foreign_instance': aws['hosts'][0]['Tags']=[]
    elif fault=='missing_disk': aws['hosts'][0]['BlockDeviceMappings']=[]; aws['hosts'][1]['BlockDeviceMappings']=[]; aws['hosts'][2]['BlockDeviceMappings']=[]
    elif fault=='foreign_attachment': aws['volumes'][0]['Attachments']=[{'InstanceId':'i-unrelated'}]
    else: aws['volumes'].pop()
    with pytest.raises(ValueError): plan(aws)
