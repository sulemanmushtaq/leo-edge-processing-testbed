# LEO edge-processing testbed (II2202, KTH)

```
 site ──access── sat1 ──isl── sat2 ──feeder── ground
 10.10.1.10   .1.20 | .2.20   .2.30 | .3.30   10.10.3.40
```
All shaping happens with `tc` inside container network namespaces; host NICs are untouched.

## Quick start (run from project root, in WSL)
```bash
docker compose -f configs/docker-compose.yml up -d --build
python3 scripts/netem.py apply starlink_baseline     # install qdiscs, writes data/raw/netem/<run_id>/applied.json
python3 scripts/netem.py verify --run-id smoke        # ping + iperf3, writes data/raw/verify/
python3 scripts/netem.py stats  --run-id smoke --label after   # tc counters -> data/raw/tc_stats/
python3 scripts/netem.py dynamic starlink_baseline --duration 300   # 15 s handover-style delay resampling
python3 scripts/netem.py show | clear
```
Profiles live in `configs/network_profiles.yaml` (`ideal`, `starlink_baseline`, `starlink_lossless`,
`starlink_multihop`, `starlink_degraded`). Delays are one-way, per link, per direction.

Note: `tc` state is lost if a container restarts — re-run `apply` after `up`/restart.

## Verified reference numbers (2026-11-01, WSL2 kernel 6.6)
| profile            | RTT avg | uplink (cap) | downlink (cap) |
|--------------------|--------:|-------------:|---------------:|
| ideal              | 0.14 ms | ~34 Gbps     | ~32 Gbps       |
| starlink_baseline  | 54.7 ms | 5.6 (8) Mbps | 7.3 (100) Mbps — TCP loss-limited by 0.5% random loss |
| starlink_lossless  | 54.9 ms | 7.5 (8) Mbps | 93 (100) Mbps  |
| starlink_multihop  | 79.2 ms | —            | —              |

## Why `gateway_mode_ipv4: nat-unprotected`?
Docker ≥ 28 adds raw-table DROP rules for packets addressed to a container that arrive on a
different bridge, which silently breaks multi-hop routing between compose networks.
