"""Lightweight, dependency-free memory probe for diagnosing OOM on constrained
hosts (e.g. Render free tier, 512Mi cgroup). Reads the process RSS and the
container's cgroup memory usage/limit straight from /proc and /sys so it works
without psutil and without a paid metrics tier."""
import logging

logger = logging.getLogger("mem")


def _read_int(path):
    try:
        with open(path, "r") as f:
            v = f.read().strip()
        if v == "max":
            return None
        return int(v)
    except Exception:
        return None


def _proc_rss_kb():
    """Resident set size of this process in KiB, from /proc/self/status."""
    try:
        with open("/proc/self/status", "r") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    # 'VmRSS:\t  123456 kB'
                    return int(line.split()[1])
    except Exception:
        return None
    return None


def _proc_threads():
    """Number of threads in this process, from /proc/self/status."""
    try:
        with open("/proc/self/status", "r") as f:
            for line in f:
                if line.startswith("Threads:"):
                    return int(line.split()[1])
    except Exception:
        return None
    return None


def _proc_cpu_seconds():
    """Total CPU seconds (user+sys) this process has burned, from /proc/self/stat.

    Comparing this between two probes (against the wall-clock gap in the log
    timestamps) tells us if the CPU is PEGGED: if cpu-delta ~= wall-delta the
    one core is maxed (real heavy work or a spin); if cpu-delta << wall-delta
    the process is mostly idle/waiting on I/O (so the stall is NOT our CPU)."""
    try:
        import os as _os
        with open("/proc/self/stat", "r") as f:
            raw = f.read()
        # 'pid (comm) state ppid ...' — comm may contain spaces/parens, so split
        # AFTER the last ')'. Post-')' tokens start at field 3 (state), so utime
        # (field 14) is index 11 and stime (field 15) is index 12.
        after = raw[raw.rfind(")") + 1:].split()
        utime = int(after[11])
        stime = int(after[12])
        try:
            hz = _os.sysconf("SC_CLK_TCK")
        except Exception:
            hz = 100
        return (utime + stime) / float(hz or 100)
    except Exception:
        return None


def _cgroup_usage_limit():
    """(usage_bytes, limit_bytes) for the container cgroup. Tries v2 then v1."""
    # cgroup v2
    usage = _read_int("/sys/fs/cgroup/memory.current")
    limit = _read_int("/sys/fs/cgroup/memory.max")
    if usage is not None:
        return usage, limit
    # cgroup v1
    usage = _read_int("/sys/fs/cgroup/memory/memory.usage_in_bytes")
    limit = _read_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    # v1 limit is often a huge sentinel when unset; treat >100GiB as "no limit"
    if limit is not None and limit > 100 * 1024 ** 3:
        limit = None
    return usage, limit


def log_mem(tag):
    """Emit one INFO line: process RSS + cgroup usage/limit + CPU secs + threads.

    cpu= is cumulative process CPU seconds; compare it across two probes against
    the wall-clock gap in the log timestamps to see if the one shared core is
    pegged. thr= is the live thread count (catches a thread explosion). Never
    raises."""
    try:
        rss_kb = _proc_rss_kb()
        usage, limit = _cgroup_usage_limit()
        cpu = _proc_cpu_seconds()
        thr = _proc_threads()
        rss_mb = f"{rss_kb / 1024:.0f}MiB" if rss_kb is not None else "?"
        cpu_s = f"{cpu:.1f}s" if cpu is not None else "?"
        thr_s = str(thr) if thr is not None else "?"
        suffix = f" cpu={cpu_s} thr={thr_s}"
        if usage is not None:
            usage_mb = f"{usage / 1024 ** 2:.0f}MiB"
            if limit:
                limit_mb = f"{limit / 1024 ** 2:.0f}MiB"
                pct = f"{usage / limit * 100:.0f}%"
                logger.info(
                    f"MEM[{tag}] rss={rss_mb} cgroup={usage_mb}/{limit_mb} ({pct}){suffix}"
                )
            else:
                logger.info(f"MEM[{tag}] rss={rss_mb} cgroup={usage_mb}/no-limit{suffix}")
        else:
            logger.info(f"MEM[{tag}] rss={rss_mb} (cgroup stats unavailable){suffix}")
    except Exception as e:
        logger.info(f"MEM[{tag}] probe failed: {e}")
