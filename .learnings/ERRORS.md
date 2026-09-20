# Errors

Command failures and integration errors.

---

## [ERR-20260918-001] macos-find-printf

**Logged**: 2026-09-18T00:00:00+08:00
**Priority**: low
**Status**: pending
**Area**: tooling

### Summary
GNU `find` option `-printf` is unavailable in the macOS environment.

### Error
```text
find: -printf: unknown primary or operator
```

### Context
- Attempted to list source files with `find ... -printf` while analyzing the repository architecture.
- The repository and working tree were unaffected.

### Suggested Fix
Use portable `find` options or `rg --files` for repository file inventories on macOS.

### Metadata
- Reproducible: yes
- Related Files: none

---
