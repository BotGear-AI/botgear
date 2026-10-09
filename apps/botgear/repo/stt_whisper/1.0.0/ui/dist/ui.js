// GENERATED FILE — built by build-service-ui.mjs from View.tsx. Do not edit; do not lint.
/* eslint-disable */
// @ts-nocheck
import { useCallback, useEffect, useRef, useState } from "react";
import { MicrophoneSourcePicker, useWsClient } from "@rlx/ui";
import { jsx, jsxs } from "react/jsx-runtime";
//#region ../botgear/repo/stt_whisper/1.0.0/ui/View.tsx
function SttWhisperView({ proxy }) {
	const wsClient = useWsClient();
	const proxyId = proxy.id ?? proxy.name ?? "";
	const T = (s) => `/stt/${proxyId}/${s}`;
	const [state, setState] = useState({});
	const [ref, setRef] = useState("");
	const [lang, setLang] = useState("");
	const [partial, setPartial] = useState("");
	const [finals, setFinals] = useState([]);
	const [filename, setFilename] = useState("");
	const refDirty = useRef(false);
	const langDirty = useRef(false);
	const filenameEdited = useRef(false);
	const sendAction = useCallback((action, args = {}) => wsClient.publish(T("control"), {
		action,
		...args
	}), [wsClient, proxyId]);
	useEffect(() => {
		if (!proxyId) return;
		const offState = wsClient.subscribe(T("state"), (f) => {
			if (f.method !== "message") return;
			const s = f.payload ?? {};
			setState(s);
			if (!refDirty.current) setRef(s.input_ref ?? "");
			if (!langDirty.current) setLang(s.language ?? "");
			if (!filenameEdited.current && s.transcript_suggested_path) setFilename(s.transcript_suggested_path);
		});
		const offText = wsClient.subscribe(T("text"), (f) => {
			if (f.method !== "message") return;
			const t = f.payload;
			const txt = (t?.text ?? "").trim();
			if (t?.final) {
				setPartial("");
				if (txt) setFinals((prev) => [...prev.slice(-49), txt]);
			} else setPartial(txt);
		});
		sendAction("list_models");
		return () => {
			offState();
			offText();
		};
	}, [
		proxyId,
		wsClient,
		sendAction
	]);
	const logRef = useRef(null);
	useEffect(() => {
		const el = logRef.current;
		if (el) el.scrollTop = el.scrollHeight;
	}, [finals, partial]);
	const applyInput = () => {
		sendAction("select_input", {
			kind: ref.trim() ? "topic" : null,
			ref: ref.trim() || null
		});
		refDirty.current = false;
	};
	const applyLanguage = () => {
		sendAction("set_language", { language: lang.trim() || null });
		langDirty.current = false;
	};
	const muted = !!state.muted;
	const listening = !!state.listening;
	const transcribing = !!state.transcribing;
	const recordingTranscript = !!state.recording_transcript;
	const models = state.models ?? [];
	const level = muted ? 0 : state.level_rms ?? 0;
	const status = state.downloading ? "downloading model…" : transcribing ? "transcribing…" : !state.ready && listening ? "starting…" : listening ? "listening" : "idle";
	return /* @__PURE__ */ jsxs("div", {
		className: "rlx-drag-handle space-y-3 p-3 text-sm text-slate-200",
		children: [
			/* @__PURE__ */ jsxs("div", {
				className: "flex items-center justify-between",
				children: [/* @__PURE__ */ jsx("span", {
					className: "font-semibold",
					children: "Speech-to-text (Whisper)"
				}), /* @__PURE__ */ jsx("span", {
					className: "text-[10px] uppercase tracking-wide text-violet-400",
					children: state.source ?? "whisper"
				})]
			}),
			/* @__PURE__ */ jsxs("label", {
				className: "block space-y-1",
				children: [/* @__PURE__ */ jsx("span", {
					className: "text-xs text-slate-400",
					children: "Microphone"
				}), /* @__PURE__ */ jsxs("div", {
					className: "flex gap-2",
					children: [/* @__PURE__ */ jsx(MicrophoneSourcePicker, {
						value: ref,
						onChange: (topic) => {
							setRef(topic);
							refDirty.current = true;
						},
						className: "nodrag nopan min-w-0 flex-1 rounded border border-slate-700 bg-slate-950 px-2 py-1 text-xs text-slate-200"
					}), /* @__PURE__ */ jsx("button", {
						type: "button",
						onClick: applyInput,
						className: "nodrag nopan rounded border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:border-slate-500",
						onPointerDownCapture: (e) => e.stopPropagation(),
						children: "Set"
					})]
				})]
			}),
			/* @__PURE__ */ jsxs("label", {
				className: "block space-y-1",
				children: [/* @__PURE__ */ jsx("span", {
					className: "text-xs text-slate-400",
					children: "Model"
				}), /* @__PURE__ */ jsxs("select", {
					value: state.model ?? "",
					onChange: (e) => sendAction("set_model", { model: e.target.value || null }),
					className: "nodrag nopan w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 text-xs",
					onPointerDown: (e) => e.stopPropagation(),
					children: [/* @__PURE__ */ jsx("option", {
						value: "",
						children: "(default model)"
					}), models.map((m) => /* @__PURE__ */ jsxs("option", {
						value: m.id,
						children: [m.label ?? m.id, m.downloaded ? "" : " ⤓"]
					}, m.id))]
				})]
			}),
			/* @__PURE__ */ jsxs("label", {
				className: "block space-y-1",
				children: [/* @__PURE__ */ jsx("span", {
					className: "text-xs text-slate-400",
					children: "Language (blank = auto-detect)"
				}), /* @__PURE__ */ jsxs("div", {
					className: "flex gap-2",
					children: [/* @__PURE__ */ jsx("input", {
						type: "text",
						value: lang,
						onChange: (e) => {
							setLang(e.target.value);
							langDirty.current = true;
						},
						placeholder: "en",
						className: "nodrag nopan w-20 rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-[11px]",
						onPointerDown: (e) => e.stopPropagation()
					}), /* @__PURE__ */ jsx("button", {
						type: "button",
						onClick: applyLanguage,
						className: "nodrag nopan rounded border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:border-slate-500",
						onPointerDownCapture: (e) => e.stopPropagation(),
						children: "Set"
					})]
				})]
			}),
			/* @__PURE__ */ jsxs("div", {
				className: "flex gap-2",
				children: [/* @__PURE__ */ jsx("button", {
					type: "button",
					onClick: () => sendAction(listening ? "stop" : "start"),
					className: `nodrag nopan flex-1 rounded px-2 py-1 text-xs font-medium ${listening ? "border border-rose-600 text-rose-300 hover:bg-rose-950/40" : "bg-violet-600 text-white hover:bg-violet-500"}`,
					onPointerDownCapture: (e) => e.stopPropagation(),
					children: listening ? "Stop" : "Start"
				}), /* @__PURE__ */ jsx("button", {
					type: "button",
					onClick: () => sendAction(muted ? "unmute" : "mute"),
					className: `nodrag nopan rounded border px-2 py-1 text-xs ${muted ? "border-amber-500 text-amber-300" : "border-slate-700 text-slate-300 hover:border-slate-500"}`,
					onPointerDownCapture: (e) => e.stopPropagation(),
					children: muted ? "Unmute" : "Mute"
				})]
			}),
			/* @__PURE__ */ jsx("div", {
				className: "h-2 w-full overflow-hidden rounded bg-slate-800",
				children: /* @__PURE__ */ jsx("div", {
					className: `h-full transition-[width] duration-75 ${transcribing ? "animate-pulse bg-violet-400" : "bg-violet-400"}`,
					style: { width: `${Math.round(level * 100)}%` }
				})
			}),
			/* @__PURE__ */ jsxs("div", {
				className: "flex items-center justify-between",
				children: [/* @__PURE__ */ jsx("span", {
					className: "text-xs text-slate-400",
					children: "Transcript"
				}), /* @__PURE__ */ jsx("button", {
					type: "button",
					onClick: () => {
						setFinals([]);
						setPartial("");
					},
					disabled: finals.length === 0 && !partial,
					className: "nodrag nopan rounded border border-slate-700 px-2 py-0.5 text-[11px] text-slate-400 hover:border-slate-500 disabled:cursor-not-allowed disabled:opacity-40",
					onPointerDownCapture: (e) => e.stopPropagation(),
					children: "Clear"
				})]
			}),
			/* @__PURE__ */ jsxs("div", {
				ref: logRef,
				className: "max-h-40 min-h-[3rem] space-y-1 overflow-y-auto rounded border border-slate-800 bg-slate-950/60 p-2 text-[12px] leading-snug",
				children: [
					finals.length === 0 && !partial && /* @__PURE__ */ jsx("div", {
						className: "text-slate-600",
						children: "…transcript appears here (after each utterance ends)…"
					}),
					finals.map((t, i) => /* @__PURE__ */ jsx("div", {
						className: "text-slate-200",
						children: t
					}, i)),
					partial && /* @__PURE__ */ jsx("div", {
						className: "text-violet-300/80 italic",
						children: partial
					})
				]
			}),
			/* @__PURE__ */ jsxs("div", {
				className: "flex items-center justify-between text-[10px] text-slate-500",
				children: [/* @__PURE__ */ jsxs("span", { children: ["queued: ", state.queued ?? 0] }), /* @__PURE__ */ jsx("span", {
					className: muted ? "text-amber-400" : state.downloading || transcribing ? "text-sky-400" : listening ? "text-emerald-400" : "text-slate-500",
					children: status
				})]
			}),
			state.last_error && /* @__PURE__ */ jsx("div", {
				className: "text-[10px] text-rose-400",
				children: state.last_error
			}),
			/* @__PURE__ */ jsxs("div", {
				className: "space-y-1 border-t border-slate-800 pt-2",
				children: [
					/* @__PURE__ */ jsx("span", {
						className: "text-xs text-slate-400",
						children: "Save transcript to file"
					}),
					/* @__PURE__ */ jsx("input", {
						type: "text",
						value: filename,
						onChange: (e) => {
							setFilename(e.target.value);
							filenameEdited.current = true;
						},
						placeholder: "auto filename",
						disabled: recordingTranscript,
						className: "nodrag nopan w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-[11px] text-slate-200 disabled:opacity-60",
						onPointerDown: (e) => e.stopPropagation()
					}),
					/* @__PURE__ */ jsxs("div", {
						className: "flex items-center gap-2",
						children: [recordingTranscript ? /* @__PURE__ */ jsx("button", {
							type: "button",
							onClick: () => sendAction("stop_recording"),
							className: "nodrag nopan rounded bg-rose-600 px-3 py-1 text-xs font-medium text-white hover:bg-rose-500",
							onPointerDownCapture: (e) => e.stopPropagation(),
							children: "Stop saving"
						}) : /* @__PURE__ */ jsx("button", {
							type: "button",
							onClick: () => {
								sendAction("start_recording", { path: filename || void 0 });
								filenameEdited.current = false;
							},
							className: "nodrag nopan rounded border border-emerald-700 px-3 py-1 text-xs font-medium text-emerald-300 hover:border-emerald-500",
							onPointerDownCapture: (e) => e.stopPropagation(),
							children: "Record to file"
						}), recordingTranscript && /* @__PURE__ */ jsxs("span", {
							className: "text-[10px] text-emerald-400",
							children: [
								"● ",
								state.transcript_lines ?? 0,
								" lines"
							]
						})]
					})
				]
			})
		]
	});
}
//#endregion
export { SttWhisperView as default };
