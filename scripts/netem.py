#!/usr/bin/env python3
"""tc-netem controller for the LEO testbed.

All shaping happens inside container network namespaces via `docker exec`
(containers have CAP_NET_ADMIN); host interfaces are never touched.

Subcommands
  apply   <profile>   clear + install qdiscs for a profile, snapshot config
  clear               remove all qdiscs from testbed interfaces
  show                print current qdiscs
  stats   [--label]   dump `tc -s -j qdisc` counters to data/raw/tc_stats/
  dynamic <profile>   apply profile, then resample access delay every interval
  verify              ping + iperf3 site<->ground, results to data/raw/verify/

Every invocation that changes state writes JSON under data/raw/ so a run can
be reconstructed from disk alone.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import ipaddress
import json
import random
import subprocess
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PROFILES_FILE = ROOT / "configs" / "network_profiles.yaml"
RAW = ROOT / "data" / "raw"


# ----------------------------------------------------------------- helpers
def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def sh(container: str, *cmd: str, check: bool = True, timeout: float = 60) -> str:
    p = subprocess.run(["docker", "exec", container, *cmd], capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"[{container}] {' '.join(cmd)}\n{p.stderr.strip()}")
    return p.stdout


def load_cfg() -> dict:
    with open(PROFILES_FILE) as f:
        return yaml.safe_load(f)


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in ("a_to_b", "b_to_a", "both"):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def resolve_profile(cfg: dict, name: str) -> dict:
    profiles = cfg["profiles"]
    if name not in profiles:
        sys.exit(f"unknown profile '{name}'. available: {', '.join(profiles)}")
    p = profiles[name] or {}
    if "inherits" in p:
        p = deep_merge(resolve_profile(cfg, p["inherits"]), {k: v for k, v in p.items() if k != "inherits"})
    return p


def iface_for(container: str, subnet: str) -> str:
    """Find the interface in `container` whose IPv4 address is in `subnet`.
    Docker doesn't guarantee eth0/eth1 ordering, so never hard-code names."""
    net = ipaddress.ip_network(subnet)
    for entry in json.loads(sh(container, "ip", "-j", "-4", "addr", "show")):
        for a in entry.get("addr_info", []):
            if ipaddress.ip_address(a["local"]) in net:
                return entry["ifname"]
    raise RuntimeError(f"{container}: no interface in {subnet}")


def endpoints(cfg: dict):
    """Yield (link, side, node, container, iface) for every shaped egress point.
    side 'a_to_b' = egress of node a on that link."""
    for link, L in cfg["links"].items():
        for side, node in (("a_to_b", L["a"]), ("b_to_a", L["b"])):
            c = cfg["containers"][node]
            yield link, side, node, c, iface_for(c, L["subnet"])


def direction_params(profile: dict, link: str, side: str) -> dict | None:
    lp = profile.get(link) or {}
    return lp.get(side) or lp.get("both")


# ----------------------------------------------------------------- tc core
def netem_args(p: dict) -> list[str]:
    hops = int(p.get("hops", 1))
    delay = float(p.get("delay_ms", 0)) * hops
    jitter = float(p.get("jitter_ms", 0))
    args = ["delay", f"{delay}ms"]
    if jitter > 0:
        args += [f"{jitter}ms"]
        if p.get("jitter_corr_pct"):
            args += [f"{p['jitter_corr_pct']}%"]
        args += ["distribution", "normal"]
    if p.get("loss_pct"):
        args += ["loss", f"{p['loss_pct']}%"]
    if p.get("extra"):
        args += str(p["extra"]).split()
    # netem's own queue must hold everything "in flight" on the delay line.
    args += ["limit", str(p.get("limit", 10000))]
    return args


def tbf_args(p: dict) -> list[str]:
    rate = p["rate"]
    bits = _rate_bits(rate)
    queue_ms = float(p.get("queue_ms", 100))
    burst = max(int(bits / 8 / 250), 3200)          # ~4 ms worth, >= 2 MTUs
    return ["rate", rate, "burst", str(burst), "latency", f"{queue_ms}ms"]


def _rate_bits(rate: str) -> float:
    r = rate.lower()
    for suf, mul in (("gbit", 1e9), ("mbit", 1e6), ("kbit", 1e3), ("bit", 1)):
        if r.endswith(suf):
            return float(r[: -len(suf)]) * mul
    return float(r)


def clear_iface(c: str, ifc: str):
    sh(c, "tc", "qdisc", "del", "dev", ifc, "root", check=False)


def apply_dir(c: str, ifc: str, p: dict):
    """root netem (delay/jitter/loss)  ->  child tbf (rate cap + queue)."""
    clear_iface(c, ifc)
    sh(c, "tc", "qdisc", "add", "dev", ifc, "root", "handle", "1:", "netem", *netem_args(p))
    if p.get("rate"):
        sh(c, "tc", "qdisc", "add", "dev", ifc, "parent", "1:", "handle", "2:", "tbf", *tbf_args(p))


def snapshot(cfg: dict) -> dict:
    out = {"ts": now_iso(), "nodes": {}}
    for node, c in cfg["containers"].items():
        out["nodes"][node] = json.loads(sh(c, "tc", "-s", "-j", "qdisc", "show") or "[]")
    return out


def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2))
    return path


# ----------------------------------------------------------------- commands
def cmd_apply(args, cfg=None, quiet=False):
    cfg = cfg or load_cfg()
    profile = resolve_profile(cfg, args.profile)
    run_id = args.run_id or f"{stamp()}_{args.profile}"
    applied = []
    for link, side, node, c, ifc in endpoints(cfg):
        p = direction_params(profile, link, side)
        if p:
            apply_dir(c, ifc, p)
            cmd = f"netem {' '.join(netem_args(p))}" + (f" | tbf {' '.join(tbf_args(p))}" if p.get("rate") else "")
        else:
            clear_iface(c, ifc)
            cmd = "(none)"
        applied.append({"link": link, "direction": side, "node": node, "iface": ifc, "params": p, "tc": cmd})
        if not quiet:
            print(f"  {link:7s} {side:7s} {node:6s} {ifc:6s} {cmd}")
    rec = {"run_id": run_id, "ts": now_iso(), "profile": args.profile, "resolved": profile,
           "applied": applied, "qdisc_snapshot": snapshot(cfg)}
    path = write_json(RAW / "netem" / run_id / "applied.json", rec)
    if not quiet:
        print(f"[apply] profile={args.profile} run_id={run_id} -> {path.relative_to(ROOT)}")
    return run_id


def cmd_clear(args):
    cfg = load_cfg()
    for _, _, node, c, ifc in endpoints(cfg):
        clear_iface(c, ifc)
        print(f"  cleared {node}:{ifc}")


def cmd_show(args):
    cfg = load_cfg()
    for node, c in cfg["containers"].items():
        print(f"== {node} ({c})")
        print(sh(c, "tc", "-s", "qdisc", "show").rstrip())


def cmd_stats(args):
    cfg = load_cfg()
    run_id = args.run_id or "adhoc"
    snap = snapshot(cfg)
    snap.update(run_id=run_id, label=args.label)
    path = write_json(RAW / "tc_stats" / run_id / f"{stamp()}_{args.label}.json", snap)
    print(f"[stats] -> {path.relative_to(ROOT)}")
    return path


def cmd_dynamic(args):
    cfg = load_cfg()
    dyn = cfg["dynamic"]
    interval = args.interval or dyn["interval_s"]
    lo, hi = dyn["delay_ms_range"]
    link = dyn["link"]
    run_id = cmd_apply(args, cfg)
    profile = resolve_profile(cfg, args.profile)
    log = RAW / "netem" / run_id / "dynamic.jsonl"
    rng = random.Random(args.seed)
    t_end = time.time() + args.duration
    print(f"[dynamic] resampling {link} delay in [{lo},{hi}] ms every {interval}s for {args.duration}s (seed={args.seed})")
    with open(log, "a") as f:
        while time.time() < t_end:
            d = round(rng.uniform(lo, hi), 2)
            for lk, side, node, c, ifc in endpoints(cfg):
                if lk != link:
                    continue
                p = dict(direction_params(profile, lk, side) or {})
                p["delay_ms"] = d
                # `change` keeps the child tbf (and its counters) intact.
                sh(c, "tc", "qdisc", "change", "dev", ifc, "root", "handle", "1:", "netem", *netem_args(p))
            f.write(json.dumps({"ts": now_iso(), "link": link, "delay_ms": d}) + "\n")
            f.flush()
            print(f"  {now_iso()} {link} delay={d}ms")
            time.sleep(min(interval, max(0, t_end - time.time())))
    print(f"[dynamic] log -> {log.relative_to(ROOT)}")


def cmd_verify(args):
    cfg = load_cfg()
    site, ground = cfg["containers"]["site"], cfg["containers"]["ground"]
    ground_ip = "10.10.3.40"
    res = {"ts": now_iso(), "run_id": args.run_id}
    res["traceroute_path"] = sh(site, "ip", "route", "get", ground_ip).strip()

    ping = sh(site, "ping", "-c", str(args.pings), "-i", "0.2", "-W", "2", ground_ip, check=False,
              timeout=args.pings * 0.2 + 30)
    res["ping_raw"] = ping
    ttls = {int(t.split("ttl=")[1].split()[0]) for t in ping.splitlines() if "ttl=" in t}
    # Default TTL 64; each forwarding node decrements. site->ground crosses
    # sat1, sat2 (+ nothing else) => expect 62. Anything else means the path leaked.
    res["ttl_seen"] = sorted(ttls)
    for line in ping.splitlines():
        if "packet loss" in line:
            res["ping_loss_pct"] = float(line.split("%")[0].split()[-1])
        if line.startswith("rtt") or line.startswith("round-trip"):
            mn, avg, mx, mdev = line.split("=")[1].split()[0].split("/")
            res["rtt_ms"] = {"min": float(mn), "avg": float(avg), "max": float(mx), "mdev": float(mdev)}

    subprocess.run(["docker", "exec", "-d", ground, "iperf3", "-s", "-1"], check=True)
    time.sleep(1)
    ip3 = ["iperf3", "-c", ground_ip, "-t", str(args.secs), "-J", "--connect-timeout", "5000"]
    up = json.loads(sh(site, *ip3, check=False, timeout=args.secs + 30) or "{}")
    subprocess.run(["docker", "exec", "-d", ground, "iperf3", "-s", "-1"], check=True)
    time.sleep(1)
    down = json.loads(sh(site, *ip3, "-R", check=False, timeout=args.secs + 30) or "{}")

    def summ(j):
        e = j.get("end", {})
        return {"mbps": round(e.get("sum_received", {}).get("bits_per_second", 0) / 1e6, 3),
                "retransmits": e.get("sum_sent", {}).get("retransmits")}
    res["uplink"], res["downlink"] = summ(up), summ(down)
    res["iperf_raw"] = {"uplink": up, "downlink": down}
    path = write_json(RAW / "verify" / f"{stamp()}_{args.run_id or 'adhoc'}.json", res)
    print(json.dumps({k: res[k] for k in ("traceroute_path", "ttl_seen", "ping_loss_pct", "rtt_ms", "uplink", "downlink") if k in res}, indent=2))
    print(f"[verify] -> {path.relative_to(ROOT)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    a = sp.add_parser("apply"); a.add_argument("profile"); a.add_argument("--run-id")
    sp.add_parser("clear"); sp.add_parser("show")
    s = sp.add_parser("stats"); s.add_argument("--run-id"); s.add_argument("--label", default="snapshot")
    d = sp.add_parser("dynamic"); d.add_argument("profile"); d.add_argument("--run-id")
    d.add_argument("--duration", type=float, default=120); d.add_argument("--interval", type=float)
    d.add_argument("--seed", type=int, default=42)
    v = sp.add_parser("verify"); v.add_argument("--run-id"); v.add_argument("--pings", type=int, default=50)
    v.add_argument("--secs", type=int, default=10)
    args = ap.parse_args()
    {"apply": cmd_apply, "clear": cmd_clear, "show": cmd_show, "stats": cmd_stats,
     "dynamic": cmd_dynamic, "verify": cmd_verify}[args.cmd](args)


if __name__ == "__main__":
    main()
