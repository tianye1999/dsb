"""Bounded, best-effort retention for safe_tap screenshots only."""
import fcntl
import os
import re
import stat
import time


_SHOT_NAME = re.compile(r"^(shot|before|after)-[0-9]+\.(jpg|jpeg|png)$")
_INDEX_NAME = re.compile(r"^shot-[0-9]+\.(jpg|jpeg|png)\.nodes\.json$")
_STAMP = ".retention-stamp"
_LOCK = ".retention-lock"
_INTERVAL = 6 * 3600


def retention_days(environ=None):
    raw = (os.environ if environ is None else environ).get("DSB_SHOT_RETENTION_DAYS", "3")
    try:
        days = int(raw)
        return days if 1 <= days <= 365 else 3
    except (TypeError, ValueError):
        return 3


def eligible(name, mode, mtime, cutoff):
    return bool(_SHOT_NAME.fullmatch(name) and stat.S_ISREG(mode) and mtime < cutoff)


def _open_dir(directory):
    if not all(getattr(os, flag, 0) for flag in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")):
        raise OSError("safe no-follow directory access is unavailable")
    return os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK)


def _open_entry(dirfd, name, flags, mode=0o600):
    return os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode, dir_fd=dirfd)


def _regular_private(fd):
    info = os.fstat(fd)
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1


def _last_run(dirfd):
    try:
        fd = _open_entry(dirfd, _STAMP, os.O_RDONLY)
    except FileNotFoundError:
        return None, True
    except OSError:
        return None, False
    try:
        if not _regular_private(fd):
            return None, False
        try:
            return float(os.read(fd, 64).decode("ascii")), True
        except (ValueError, UnicodeError):
            return None, True
    except OSError:
        return None, False
    finally:
        os.close(fd)


def _write_stamp(dirfd, now, deadline):
    if time.monotonic() >= deadline:
        return False
    name = ".retention-tmp-" + os.urandom(8).hex()
    fd = _open_entry(dirfd, name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    pending = True
    try:
        if not _regular_private(fd):
            raise OSError("temporary stamp is not a private regular file")
        payload = ("%.6f\n" % now).encode("ascii")
        if os.write(fd, payload) != len(payload):
            raise OSError("short stamp write")
        os.close(fd)
        fd = None
        if time.monotonic() >= deadline:
            return False
        os.replace(name, _STAMP, src_dir_fd=dirfd, dst_dir_fd=dirfd)
        pending = False
        return True
    finally:
        if fd is not None:
            os.close(fd)
        if pending:
            try:
                os.unlink(name, dir_fd=dirfd)
            except FileNotFoundError:
                pass


def _protected_names(directory, protect):
    parent = os.path.abspath(directory)
    names = set()
    for item in protect or ():
        if item is None:
            continue
        path = os.path.abspath(os.fspath(item))
        if os.path.dirname(path) == parent:
            names.add(os.path.basename(path))
            names.add(os.path.basename(path) + ".nodes.json")
    return names


def prune_shots(directory, days=None, dry_run=False, throttle=True, now=None,
                max_files=5000, max_seconds=3.0, protect=None):
    """Return counts; deleted/bytes mean would-delete counts in dry-run mode."""
    result = {"deleted": 0, "bytes": 0, "kept": 0, "errors": 0,
              "skipped_reason": None, "truncated": False, "scanned": 0}
    dirfd = lockfd = None
    try:
        if days is None:
            days = retention_days()
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
            result["skipped_reason"] = "invalid_days"
            return result
        now = time.time() if now is None else now
        deadline = time.monotonic() + max_seconds
        try:
            dirfd = _open_dir(directory)
        except FileNotFoundError:
            return result
        if not dry_run:
            lockfd = _open_entry(dirfd, _LOCK, os.O_RDWR | os.O_CREAT)
            if not _regular_private(lockfd):
                result["errors"] += 1
                result["skipped_reason"] = "invalid_lock"
                return result
            try:
                fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                result["skipped_reason"] = "locked"
                return result
            if throttle:
                last, safe_stamp = _last_run(dirfd)
                if not safe_stamp:
                    result["errors"] += 1
                if last is not None and 0 <= now - last < _INTERVAL:
                    result["skipped_reason"] = "throttled"
                    return result
        protected = _protected_names(directory, protect)
        cutoff = now - days * 86400
        considered = 0

        def remove(name, info):
            if not dry_run:
                os.unlink(name, dir_fd=dirfd)
            result["deleted"] += 1
            result["bytes"] += info.st_size

        with os.scandir(dirfd) as entries:
            for entry in entries:
                if time.monotonic() >= deadline:
                    result["truncated"] = True
                    break
                result["scanned"] += 1
                is_index = bool(_INDEX_NAME.fullmatch(entry.name))
                if not is_index and not _SHOT_NAME.fullmatch(entry.name):
                    continue
                try:
                    info = os.stat(entry.name, dir_fd=dirfd, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    if entry.name in protected:
                        result["kept"] += 1
                        continue
                    if is_index:
                        try:
                            os.stat(entry.name[:-len(".nodes.json")], dir_fd=dirfd, follow_symlinks=False)
                        except FileNotFoundError:
                            if info.st_mtime < cutoff:
                                remove(entry.name, info)  # Orphans do not consume screenshot slots.
                            else:
                                result["kept"] += 1
                        continue
                    if info.st_mtime >= cutoff:
                        result["kept"] += 1
                        continue
                    if considered >= max_files:
                        result["truncated"] = True
                        break
                    considered += 1
                    remove(entry.name, info)
                    sidecar = entry.name + ".nodes.json"
                    if _INDEX_NAME.fullmatch(sidecar) and sidecar not in protected:
                        try:
                            side_info = os.stat(sidecar, dir_fd=dirfd, follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        if stat.S_ISREG(side_info.st_mode):
                            remove(sidecar, side_info)  # Same lifecycle, even if its mtime is newer.
                except FileNotFoundError:
                    continue
                except OSError:
                    result["errors"] += 1
        if time.monotonic() >= deadline:
            result["truncated"] = True
        if throttle and not dry_run and not result["truncated"] and not result["errors"]:
            if not _write_stamp(dirfd, now, deadline):
                result["truncated"] = True
    except OSError:
        result["errors"] += 1
        result["skipped_reason"] = "io_error"
    except Exception:
        result["errors"] += 1
        result["skipped_reason"] = "error"
    finally:
        for fd in (lockfd, dirfd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    result["errors"] += 1
    return result


def directory_usage(directory, now=None, max_entries=5000, max_seconds=3.0):
    """Count matching regular screenshots in one directory, with a bounded scan."""
    result = {"files": 0, "bytes": 0, "oldest_days": None, "errors": 0,
              "truncated": False, "scanned": 0, "skipped_reason": None}
    dirfd = None
    try:
        now = time.time() if now is None else now
        deadline = time.monotonic() + max_seconds
        try:
            dirfd = _open_dir(directory)
        except FileNotFoundError:
            return result
        oldest = None
        with os.scandir(dirfd) as entries:
            for entry in entries:
                if time.monotonic() >= deadline:
                    result["truncated"] = True
                    break
                if _INDEX_NAME.fullmatch(entry.name):
                    continue
                if result["scanned"] >= max_entries:
                    result["truncated"] = True
                    break
                result["scanned"] += 1
                if not _SHOT_NAME.fullmatch(entry.name):
                    continue
                try:
                    info = os.stat(entry.name, dir_fd=dirfd, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    result["files"] += 1
                    result["bytes"] += info.st_size
                    oldest = info.st_mtime if oldest is None else min(oldest, info.st_mtime)
                except OSError:
                    result["errors"] += 1
        if oldest is not None:
            result["oldest_days"] = max(0, (now - oldest) / 86400)
    except Exception:
        result["errors"] += 1
        result["skipped_reason"] = "io_error"
    finally:
        if dirfd is not None:
            try:
                os.close(dirfd)
            except OSError:
                result["errors"] += 1
    return result
