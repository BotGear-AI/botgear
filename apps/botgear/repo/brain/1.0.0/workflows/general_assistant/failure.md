General assistant session did not complete. See the failure reason on
the run record — typical causes: the conversation service (stt_local/
speech_local, or a chat-capability service) went offline, listen/speak
erroring repeatedly, or an ollama request failing because the tool
catalog grew too large for the configured context window
(ollama_num_ctx in brain.yml — raised to 32768 when this workflow was
built; check actual prompt_eval_count in requests.jsonl for the run if
this recurs).
