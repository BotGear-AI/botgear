# TODO — slam: remaining work

The `slam` service (Phases 0–3) and visual odometry (Phases 4a/4b) are
built — documented in `repo/slam/1.0.0/README.md`, including the
camera-mount calibration lessons from `worke`. Verified 2026-10-10; open:

## Real-hardware smoke tests
- **Mapping + dead-reckoning (Phase 2).** Push `worke` around a real room
  by hand (motors off) and confirm the traced outline matches the room;
  then a driven run. Needs `wheel_speed_calibration_m_per_s` measured
  (drive a known distance at a known commanded output). Several geometry
  bugs were already found and fixed on the real robot; this end-to-end
  pass hasn't been done.
- **Test gap:** no backend test exercises the y/row axis
  (`test_slam_backend.py` scans all use `bearing_deg=0`), the axis where
  the rotation-direction bug lived. Add one before calling the smoke test
  done.
- **Visual odometry (Phase 4c).** Push `worke` by hand over a tape-measured
  path, pan/tilt at neutral, motors off; compare the recovered trajectory to
  ground truth; repeat driven. Only synthetic data has exercised
  `odometry_source: visual_flow` so far — the last live attempt saw all-zero
  depth (camera pointing/lighting). Nonzero mount tilt/yaw is only
  round-trip-verified, so this also confirms the absolute sign convention.

## Tuning (needs real data from the above)
- `visual_odometry_min_matches` (placeholder 8) and the RANSAC inlier
  threshold.
- `feature_flow_rate_hz` (5.0): odometry smoothness vs fit cost + bus traffic.
- `FeatureTracker` motion estimator — `LUCAS_KANADE_OPTICAL_FLOW` vs
  `HW_MOTION_ESTIMATION`; expose as an oakd config knob rather than
  hardcoding one.
- Residual false positives at the extreme edge columns (±30°).

## Later (not scheduled)
- **Fused odometry (Phase 4d):** weight visual vs commanded deltas by RANSAC
  inlier confidence instead of the hard per-tick fallback.
- `BreezySlamBackend` (scan-matching drift mitigation) once its from-source
  install is verified on aarch64.
- Per-frame pan/tilt compensation (feed commanded servo angles into the
  camera→robot transform) so the head can move while mapping.
- Loop closure, path-planning integration, rectangular footprint, growing
  map bounds.
