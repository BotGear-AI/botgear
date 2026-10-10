"""slam_service — cheap 2D occupancy mapping for worke.

See README.md for the full design. Phase 1: this
package is the core algorithm layer only (config, geometry/projection
math, the pluggable mapping-backend interface + the hand-rolled v1
implementation) — no bus/SubprocessService wiring yet, that's Phase 2.
Deliberately dependency-light: numpy only (+ rlx_bus for the config
base class), no OpenCV, no depthai — this package never touches a
camera or a depth device directly, it only consumes already-decimated
data over the bus (once Phase 2 wires that up).
"""
__version__ = "1.0.0"
