# Stop, retain or retire your own Learner Lab deployment

Use your own account/region and exact deployment IDs. This procedure distinguishes
an ordinary shutdown from permanent retirement. Do not delete the shared course
VPC/subnet, default security groups, `vockey`, `LabInstanceProfile`, another
student's deployment or TX's original manually provisioned hosts.

## Ordinary shutdown: preserve progress

For a deployment created by the Docker launcher:

```powershell
.\infrastructure\aws\run-lab.ps1 -Command status -Stack 'YOUR_STACK'
.\infrastructure\aws\run-lab.ps1 -Command stop -Stack 'YOUR_STACK' -AccountId 'YOUR_12_DIGIT_ACCOUNT'
```

Confirm all three instances are stopped, then end the session through AWS
Academy's **End Lab** control. `stop` does not terminate instances, delete data or
end the Academy session. Follow your course's retention rules. Stopped/retained
EBS storage can still consume credit; use permanent retirement only when its data
is no longer needed. See [AWS EBS retention behavior](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/preserving-volumes-on-termination.html).

## Inventory before permanent retirement

1. Obtain the deployment owner's agreement on the exact stack/data being retired.
   Pause writers, export fresh cloud/A/B backups, verify checksums and isolated
   restores, and keep approved private off-host copies. See [BACKUPS.md](BACKUPS.md).
   If already stopped, decide whether retained data needs a backup before starting
   only those hosts; there is no need to start hosts just to inventory them.
2. While both stacks and all three instances still exist, save a new private
   read-only inventory:

   ```powershell
   $inventoryPath = Join-Path $env:TEMP ('crisisguard-retirement-' + [guid]::NewGuid().ToString('N') + '.private.json')
   .\infrastructure\aws\run-lab.ps1 -Command cleanup-plan -Stack 'YOUR_STACK' -OutputFile $inventoryPath
   Get-Content -LiteralPath $inventoryPath
   ```

   Use the current temporary lab credentials as in [AWS_LAUNCHER.md](AWS_LAUNCHER.md).
   No archive, SSH key, instance start or account mutation is required. The output
   contains resource identifiers, not source objects or database contents. Keep it
   private. `-OutputFile` refuses overwrites and reserves the path before AWS access;
   a failed run may leave an empty file. Choose a fresh filename when retrying.
3. Check account/region, both stack ARNs, the three instance IDs, attached volume
   IDs/sizes/encryption/`DeleteOnTermination`, security groups and source bucket.
   The command requires matching launcher Project tags, instance stack ownership
   and the expected resource structure, and follows all stack-resource pages.
   Foreign, changing or incomplete resources stop the inventory.

The inventory is a point-in-time observation, **not authorization to delete**.
It records disks attached to the selected instances; manually added disks need
their own provenance review. It is not an orphan finder and does not enumerate
detached disks, snapshots, copied backups or resources created outside these
stacks. An old manual deployment has no matching launcher stacks: identify its
resources separately using the owner's deployment records, not guessed names.

## Retire only the reviewed resources

These are manual AWS console actions, after backups and exact-ID review:

1. In **CloudFormation**, select the reviewed main stack ARN and delete that stack.
   Wait for `DELETE_COMPLETE`; inspect Events if deletion fails. Do not force-delete
   unrelated dependencies or launch a replacement copy as a workaround.
2. In **EC2**, confirm its three instance IDs are terminated. The launcher sets
   `DeleteOnTermination=false` on its root disks: do not assume the databases were
   erased when their instances terminated.
3. In **EC2 → Volumes**, find each recorded volume ID. Confirm it is detached,
   `available`, still the reviewed disk and no longer needed. Delete only the
   individually approved disks. Never select all available volumes in the lab.
4. In **CloudFormation**, delete the reviewed `YOUR_STACK-source` stack. Its bucket
   has `DeletionPolicy: Retain`, so stack deletion preserves the bucket. In **S3**,
   select the exact recorded bucket, review whether anything must be retained,
   empty it and delete it only after approval. If versioning was enabled later,
   include retained versions/delete markers in that review; do not use a recursive
   wildcard against other buckets. Retained resources continue to exist outside
   a deleted stack. See [AWS CloudFormation DeletionPolicy](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-attribute-deletionpolicy.html).
5. Check both exact stack statuses, three instance IDs, recorded volume IDs,
   bucket and deployment security groups. Also review any snapshots/public IP
   allocations/extra resources you added separately, using their own ownership
   evidence. Do not delete a shared resource just because it has a similar name.
6. Save a sanitized retirement record: reviewed account/region, deployment name,
   backup verification references, approved exact resources, final statuses and
   reviewer/date. Keep raw inventories/backup locations private. Remove only the
   dedicated temporary credential file used for the session when finished, then
   end Learner Lab.

This launcher deliberately has **no automated delete command**. `cleanup-plan`
uses only STS/CloudFormation/EC2 read operations and neither deletes nor approves
deletion. Unit tests use fake clients; they do not prove a live teardown. Capture
inventory before retiring stacks/instances, because the original attachment
mapping will no longer be available afterward.
