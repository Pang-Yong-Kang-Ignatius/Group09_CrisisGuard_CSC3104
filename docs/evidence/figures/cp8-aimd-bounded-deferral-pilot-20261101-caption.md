# Experimental AIMD retry comparison: single paired pilot

Bars show original-backlog Cloud drain upper observer endpoints: failure backoff
63.343 s and opt-in bounded admission-deferral retry 33.828 s,
an observed 46.60% reduction. Both arms used AIMD, `pressure-experimental`,
84 authenticated Patient read requests/s, the same 88-event balanced dependency
graph and paired seed 60202. There is one pair, not a statistical sample across
seeds; no statistical significance or confidence interval is established. No
statistical error bars are drawn. Lower observer endpoints accompany the CSV
and are measurement bounds, not statistical uncertainty intervals.

Adverse results remain part of this comparison: Hospital B routine workflow p95
worsened **40.95%** (24.000 to 33.828 s), and full-run end-to-end live SLO violation
rate increased **1.726 percentage points** (99/5,325 = 1.859% to 102/2,845 = 3.585%).
Both predefined first 10-second windows after each trial's recorded all-Cloud
readiness had **zero observed violations among 840 issued requests**; successful
client p95 was approximately 16 ms. Faster drain shortened the full-run denominator
while startup violation counts changed from 99 to 102. The fixed-window result
does not replace the full-run outcome or demonstrate protection under overload.
End-to-end violations are non-200 responses, transport failures or latency >250 ms,
counted once per issued request. Both generators had zero skips and exact ledgers.

These experimental policies require explicit opt-in; production defaults and the
original negative H1/H2 findings are unchanged. Absolute 84 RPS is not a maximum
mixed-workload capacity claim. The pilot has no constant-rate or RR arm and does
not establish Appendix D3 acceptance. A compatible final-policy constant-rate
comparison, independent holdout and separate RR urgent comparison remain necessary.

Source assessment: `docs/evidence/aimd-deferral-pilot-review-20261101.json`.
Original summaries: `evaluation/results/aimd-deferral-pilot-20261101-202315/run-{failure-backoff,bounded-deferral}/summary.json`.
The builder recomputes the assessment, checks both raw archives against summaries,
and verifies source hashes before plotting. The CSV retains the actual measured
values; rounding to three decimals is display-only.

Reproduce from the repository root into a new directory:

```powershell
.\.venv311\Scripts\python.exe -m docs.evidence.plot_aimd_deferral_pilot_20261101 --output-dir evaluation/results/NEW-deferral-figure
```
