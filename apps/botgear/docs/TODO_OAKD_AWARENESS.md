# worke Robot Awareness Strategy — OAK-D Surroundings + Navigation + Interaction

Status: **strategy — high level, not an implementation plan.** Companion
to `repo/oakd/1.0.0/README.md` (which covers the `oakd` service itself:
RGB + depth streaming, v1 shipped). This doc is the roadmap for *using* that
service to make `worke` aware of its surroundings and, eventually,
interact with them — without the vision pipeline competing with the
robot's safety-critical watchdog for the same host's CPU.

Status verified 2026-10-10: Level 0 shipped; Branch B is the `slam`
service (in development — `repo/slam/1.0.0/README.md`); Level 1, Branch A, the
watchdog-priority work in §3 and the §4 interaction milestone are not
started.

No phase here is scheduled or approved for implementation. Each phase
gets its own proper design pass (research, plan, review — same process
as `repo/oakd/1.0.0/README.md`) when it's actually taken up.

---

## 1. The core tension, and the core lever

Processing a depth camera's full data stream (frames, disparity maps,
point clouds) on the host is genuinely expensive — exactly the kind of
load that can starve a time-sensitive safety watchdog if they're
sharing the same CPU. But the OAK-D's entire reason to exist is that its
expensive work — stereo depth computation, neural-network inference —
runs **on the Myriad X VPU itself**, not the host. The strategy below is
built on one lever, applied repeatedly: **keep pushing the boundary of
"what runs on-device" outward, and only let small, already-processed,
low-bandwidth results cross the USB cable.** Never let "the robot needs
to be more aware" become "the host needs to run more image processing."

---

## 2. The awareness ladder — cheapest to most expensive

Not a strict single-file line — Level 1 forks into **two independent
branches** that don't depend on each other (map first, detect first, or
both at once — whichever solves the actual near-term problem). Each
level/branch is a real, standalone capability, not a prerequisite you
must fully build before the next one is useful.

### Level 0 — shipped today
RGB + depth streamed for a human to look at (`oakd` v1,
`repo/oakd/1.0.0/README.md`). Zero autonomy. Host cost is just JPEG encode +
MJPEG push, already proven cheap enough to coexist with everything else
running on `worke`.

### Level 1 — on-device proximity awareness ("virtual bumper")
DepthAI's `SpatialLocationCalculator` node computes min/average depth
over a defined ROI **on the device by default** — confirmed supported
on RVC2 (this Lite's chip), and multiple ROIs can run off one node
([Luxonis docs](https://docs.luxonis.com/projects/api/en/latest/components/nodes/spatial_location_calculator/)).
Define a handful of zones (left/center/right × near/far, or whatever
grid suits `worke`'s geometry) and the VPU does the math — the host
receives a few floats a couple times a second
(`{"zone": "center", "closest_m": 0.4}`), never a depth image.

This is the highest value-per-CPU-cycle step in the whole ladder: no
neural-network model-management story needed (unlike the detection
branch below), cheap enough to run continuously, and immediately useful
as an *advisory* input even before any mapping or detection exists.
Natural next step after `oakd` v1 — the plumbing (a low-rate
telemetry-style bus topic) already matches the
`publishes_telemetry`/`telemetry()` pattern `oakd`'s filter framework
already has (see `repo/oakd/1.0.0/README.md` §3.7's note that this is exactly what
future on-device work slots into).

After Level 1, two branches — **neither is a prerequisite for the
other**:

### Branch A (from Level 1) — on-device object/person detection
A spatial detection network (MobileNet-SSD class model, the same tier
`repo/oakd/1.0.0/README.md` §3.7 already scoped as "Phase 2 — not v1") fuses
detection + depth **on-device**. The host receives a short list —
`[{"label": "person", "x": 0.3, "y": 0.0, "z": 1.8}]` — at a low rate,
never raw frames. Same low-bandwidth-result principle as Level 1, just
with a model file involved (the model-management questions
`repo/oakd/1.0.0/README.md` already flagged: bundle a default, let the operator
supply a path, or auto-download — still an open design question when
this phase is actually taken up). Feeds directly into §4's interaction
milestone.

### Branch B (from Level 1) — cheap 2D occupancy mapping — now its own service, `slam`
**Cheaper than Branch A** (no neural network, no model-management story
at all — pure geometry) and independent of it. The technique: collapse
the depth image into a 2D range scan using known priors — gravity, a
flat floor, and the camera's *fixed* mount geometry relative to the
robot chassis (known once via calibration, not detected per-frame) —
then accumulate those scans into a classic 2D occupancy grid as the
robot moves. Same category of technique as ROS's
`depthimage_to_laserscan` + simple grid mapping. Deliberately **not**
full visual SLAM: no dense feature tracking, no loop closure, no drift
correction — the map will drift over long/complex traversals, and that
tradeoff is what keeps it cheap.

This got developed into a full design: **`repo/slam/1.0.0/README.md`** — a new
service (`slam`) that owns robot dimensions, camera mount extrinsics,
and odometry fusion, deliberately kept dependency-light (no OpenCV, no
depthai — just numpy + trig) so it stays low-resource by construction,
not by discipline. Real constraint that design accounts for explicitly:
**this OAK-D Lite has no IMU** (`getConnectedIMU()` returns empty on
the actual unit — see `repo/oakd/1.0.0/README.md`'s resolved open question) *and*
`motor_control`/`sabertooth` are confirmed strictly open-loop (no wheel
encoders anywhere in this codebase today) — so odometry is open-loop
dead-reckoning from commanded output, a named accuracy limitation, not
a hidden one. See `repo/slam/1.0.0/README.md` for the full design.

### Level 4 — dense/semantic SLAM
Genuinely compute-intensive — dense mapping, semantic scene
understanding, real loop closure. Not a near-term goal. If ever wanted,
it belongs on separate/dedicated compute, not sharing a host with the
safety watchdog. Not scoped further here.

---

## 3. Protecting the safety watchdog — architectural, not incidental

This has to be designed in from the start, not bolted on once a level
starts causing problems:

- **Process isolation already exists** — `oakd` runs as its own
  subprocess (per `repo/oakd/1.0.0/README.md`'s design), which avoids GIL/asyncio-
  loop contention with the main backend. It does **not** by itself
  prevent host-level CPU or USB-bandwidth contention, which is the
  actual risk here.
- **OS scheduling priority**: give whatever process owns the watchdog a
  higher priority (nice level, possibly real-time scheduling) than the
  vision subprocess. Cheap, concrete, and independent of anything else
  in this doc — worth doing regardless of which awareness level is live.
- **Bound the rate, not just the content**: Level 1/Branch A/Branch B
  outputs should all publish at a modest fixed rate (a few Hz is far
  more than a mobile robot's reaction time needs), not at camera
  frame-rate. This caps worst-case cost regardless of how fast the
  camera itself runs.
- **Most important: keep "obstacle avoidance" and "the watchdog"
  conceptually and architecturally separate.** The watchdog's job is
  liveness/fail-safe — are we still receiving commands, did something
  hang — and that job must keep working even if `oakd`/`slam` crashes,
  lags, or was never started. Vision-derived awareness is an *advisory
  navigation/behavior input*, not a safety-critical stop path.
  Never let the estop's correctness depend on the camera pipeline being
  alive. A slow or dead vision subprocess should degrade the robot's
  *behavior* (it stops reacting to things it can't see), never mask or
  delay the watchdog's own independent fail-safe action.

---

## 4. Interaction — the cheapest real path, reusing what's already built

"At some point interact with them" doesn't need new infrastructure.
Branch A (person detection) plugs directly into work already shipped
this session: the `messaging` capability (`packages/rlx_messaging/README.md`) and
the brain's `conversation_session` workflow, which already knows how to
run a `listen`/`speak` loop against `chat`/`speech_local`/etc.

Concrete shape: a person-detected event from Branch A's low-rate bus
topic triggers the brain's conversation workflow (same trigger
mechanism already validated for chat's keyword-driven flows) →
`speech_local.speak(...)` greets them. No new subsystem — just a bus
subscription bridging an `oakd` detection topic into an existing
workflow trigger. This is the natural first "interaction" milestone
specifically because it costs almost nothing beyond Branch A itself.

---

## 5. Suggested order

1. **Level 1** (proximity zones) — cheap, immediately useful, arguably
   *strengthens* rather than competes with the watchdog since it's a
   legitimate additional safety-adjacent signal.
2. **Branch B** (`slam` — 2D occupancy mapping, `repo/slam/1.0.0/README.md`) —
   currently the most-designed of the two branches and the user's
   stated near-term priority; cheaper than Branch A (no NN/model
   management) and independent of it.
3. **Branch A** (person/object detection) — unlocks richer awareness
   and the interaction hook in §4, whenever it's picked up; doesn't
   need Branch B done first.
4. **§4 interaction milestone** — greet-on-detection, reusing the brain
   workflow engine as-is; depends on Branch A, not Branch B.
5. **Level 4** — not scoped; would need its own hardware conversation
   first.

Each of these, when picked up, gets the same treatment `repo/oakd/1.0.0/README.md`
got: research grounded against the real API/hardware, a written design,
review before code.
