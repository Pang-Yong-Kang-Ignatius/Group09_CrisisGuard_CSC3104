# Checkpoint 7B: isolated AWS AIMD transitions

**Result: PASS.** On 2026-10-29, revision `f128ac0f2b6e551af3e03cb3e3d44262b451195a` ran on one disposable Ubuntu `t3.medium` in `us-east-1`. The test used fresh Compose PostgreSQL and Cloud services, generated private test credentials, and synthetic controller tickets. It did not connect to the existing Cloud or Hospital instances.

| Step | Controller state | Effective rate | Telemetry | Grant result |
| --- | --- | ---: | --- | --- |
| Fresh baseline | Normal | 2.0 RPS | Fresh | No grant yet |
| Patient latency set to 300 ms in its real metrics response | Caution | 1.0 RPS | Fresh | No grant yet |
| Patient timestamp set 30 seconds old | Recovery | 0.5 RPS | Stale | Ticket retained; issue denied (`telemetry_unavailable`) |
| Restore unmodified metrics | Caution, then Normal | 0.7, then 2.0 RPS | Fresh | First synthetic grant issued; immediate second issue denied (`rate_limit`); second grant issued after pacing interval |

The observed sequence proves multiplicative rate decrease, additive recovery, stale telemetry denial, and bounded grant pacing on AWS compute. The fault proxy forwarded actual Patient, Emergency, and Admin `/metrics` responses and changed only the specified Patient field. This is controlled transition evidence, not representative load calibration or production deployment validation.

The temporary instance `i-0f813e0b5138a9851` was terminated. Its 30 GiB gp3 root volume `vol-08890a7b96c69d3f4` and test security group `sg-0533aea3e6b8d8183` were verified absent. The three existing instance IDs were verified running after cleanup. The instance used no IAM profile and a new SSH-only security group. Its host key was pinned from authenticated EC2 console output before SSH.

Reproduce on a fresh, isolated Ubuntu host with Docker and Compose using a source archive of the revision above:

```bash
python3 docs/evidence/cp7b-aws-adaptive-transitions-20261029.py \
  --revision f128ac0f2b6e551af3e03cb3e3d44262b451195a \
  --output /home/ubuntu/cp7b-aws-transitions.json
```

The [sanitized run evidence](cp7b-aws-adaptive-transitions-20261029.json) contains 44 state, rate, freshness, and decision snapshots, resource identities, the source archive checksum, and cleanup checks. A first preflight attempt stopped before Compose because the local override file was absent from the tracked source archive; the script now treats that file as optional. One completed run passed. AWS charges are subject to final billing; the approved two-hour upper estimate was about US$0.10–0.12 plus transfer and tax.
