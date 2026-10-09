# fast_decision — System One decision service

In-process Botgear service that performs sub-150ms machine-native evaluations using TypeSafe AI (Jev) or deterministic offline heuristics.

```
type:     fast_decision
version:  1.0.0
language: builtin (in-process)
```

## Actions (`/fast_decision/<id>/control`)
- `evaluate(state, questions)`: Evaluates state against arbitrary typed questions (`Noul`, `Choice`, `Score`).
- `decide_noul(state, instructions)`: Evaluates a binary assertion (returns 0.0 to 1.0).
- `decide_choice(state, instructions, options)`: Categorical selection with confidence.
- `decide_score(state, instructions, levels)`: Ordinal rubric scoring with confidence.
- `evaluate_lighting(utterance, registered_lights)`: Specialized fast reflex for voice lighting commands.
- `evaluate_pan_tilt(utterance)`: Specialized fast reflex for pan-tilt head gaze commands.
