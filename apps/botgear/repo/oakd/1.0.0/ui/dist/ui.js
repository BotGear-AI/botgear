// GENERATED FILE — built by build-service-ui.mjs from View.tsx. Do not edit; do not lint.
/* eslint-disable */
// @ts-nocheck
import { useCallback, useEffect, useMemo, useState } from "react";
import { useActiveRuntime, useWsClient } from "@rlx/ui";
import { jsx, jsxs } from "react/jsx-runtime";
//#region ../botgear/repo/oakd/1.0.0/ui/View.tsx
function OakdFullView({ proxy }) {
	const wsClient = useWsClient();
	const { connection } = useActiveRuntime();
	const proxyId = proxy.id ?? proxy.name ?? "";
	const stateTopic = `/oakd/${proxyId}/state`;
	const controlTopic = `/oakd/${proxyId}/control`;
	const rgbStreamId = `oakd/${proxyId}/rgb`;
	const depthStreamId = `oakd/${proxyId}/depth`;
	const [state, setState] = useState({});
	const [reloadTick, setReloadTick] = useState(0);
	const [monoMode, setMonoMode] = useState("off");
	const [snapshotOpen, setSnapshotOpen] = useState(false);
	const [snapshotPayload, setSnapshotPayload] = useState(null);
	const [snapshotError, setSnapshotError] = useState(null);
	const [snapshotPending, setSnapshotPending] = useState(false);
	useEffect(() => {
		if (!proxyId) return;
		return wsClient.subscribe(stateTopic, (f) => {
			if (f.method !== "message") return;
			const p = f.payload;
			if (p && typeof p === "object") setState(p);
		});
	}, [
		proxyId,
		stateTopic,
		wsClient
	]);
	const streamUrl = useCallback((streamId) => {
		if (!connection) return null;
		const token = connection.getAccessToken();
		if (!token) return null;
		return `${connection.url}/v1/stream/${encodeURIComponent(streamId)}/mjpeg?token=${encodeURIComponent(token)}&_=${reloadTick}`;
	}, [connection, reloadTick]);
	const rgbUrl = useMemo(() => streamUrl(rgbStreamId), [streamUrl, rgbStreamId]);
	const depthUrl = useMemo(() => streamUrl(depthStreamId), [streamUrl, depthStreamId]);
	const monoStreamId = state.mono_stream_camera ? `oakd/${proxyId}/mono_${state.mono_stream_camera}` : null;
	const monoUrl = useMemo(() => monoStreamId ? streamUrl(monoStreamId) : null, [streamUrl, monoStreamId]);
	const sendAction = useCallback((action, args = {}) => {
		wsClient.publish(controlTopic, {
			action,
			...args
		});
	}, [wsClient, controlTopic]);
	const onSetMonoMode = useCallback((mode) => {
		setMonoMode(mode);
		const wantCamera = mode === "off" ? null : mode;
		if (wantCamera !== (state.mono_stream_camera ?? null)) {
			sendAction("set_mono_stream", { camera: wantCamera });
			setReloadTick((t) => t + 1);
		}
	}, [sendAction, state.mono_stream_camera]);
	const onConnect = useCallback((e) => {
		e?.preventDefault();
		sendAction("connect");
		setReloadTick((t) => t + 1);
	}, [sendAction]);
	const onDisconnect = useCallback(() => sendAction("disconnect"), [sendAction]);
	const onToggleAutoConnect = useCallback((checked) => sendAction("set_auto_connect", { enabled: checked }), [sendAction]);
	const onSnapshot = useCallback(() => {
		if (snapshotPending) return;
		const requestId = typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `snap-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
		setSnapshotOpen(true);
		setSnapshotPending(true);
		setSnapshotPayload(null);
		setSnapshotError(null);
		const off = wsClient.subscribe(`/oakd/${proxyId}/snapshot`, (f) => {
			if (f.method !== "message") return;
			const p = f.payload;
			if (!p || p.request_id !== requestId) return;
			off();
			window.clearTimeout(timeoutId);
			setSnapshotPending(false);
			if (p.error) setSnapshotError(p.error);
			else setSnapshotPayload(p);
		});
		const timeoutId = window.setTimeout(() => {
			off();
			setSnapshotPending(false);
			setSnapshotError("snapshot timed out — no response from oakd service in 5s");
		}, 5e3);
		sendAction("snapshot", { request_id: requestId });
	}, [
		snapshotPending,
		wsClient,
		proxyId,
		sendAction
	]);
	const serviceRunning = proxy.status === "running" || proxy.status === "starting";
	const connected = !!state.connected;
	return /* @__PURE__ */ jsxs("div", {
		className: "flex min-w-[560px] flex-col gap-2 p-3 text-xs",
		onPointerDown: (e) => e.stopPropagation(),
		children: [
			/* @__PURE__ */ jsxs("div", {
				className: "rounded border border-slate-800 bg-slate-900/40 p-2",
				children: [
					/* @__PURE__ */ jsxs("div", {
						className: "flex flex-wrap items-center gap-x-4 gap-y-1",
						children: [
							/* @__PURE__ */ jsx(Field, {
								label: "status",
								value: connected ? "● connected" : state.retrying ? "○ reconnecting…" : "○ disconnected",
								valueClass: connected ? "text-emerald-400" : state.retrying ? "text-amber-400" : "text-rose-400",
								hint: state.retrying ? "Auto-connect is on — retrying every few seconds in the background (replug the camera, or a bad cable/port at boot both recover on their own)." : void 0
							}),
							/* @__PURE__ */ jsx(Field, {
								label: "device",
								value: state.device_name ?? "—"
							}),
							/* @__PURE__ */ jsx(Field, {
								label: "mxid",
								value: state.mxid ?? "—",
								mono: true
							}),
							/* @__PURE__ */ jsx(Field, {
								label: "usb",
								value: state.usb_speed ?? "—",
								valueClass: state.usb_speed && !["SUPER", "SUPER_PLUS"].includes(state.usb_speed) ? "text-amber-400" : void 0,
								hint: "OAK-D wants USB3 (SUPER/SUPER_PLUS). Anything else (HIGH = USB2) means reduced bandwidth — expect a choppier/less reliable stream. Check the cable/port, not botgear config."
							}),
							/* @__PURE__ */ jsx(Field, {
								label: "imu",
								value: state.has_imu == null ? "unknown" : state.has_imu ? "present" : "not present",
								hint: "Queried from the device at connect time — OAK-D Lite units vary on whether they shipped with an IMU, so this is reported rather than assumed."
							}),
							/* @__PURE__ */ jsx(Field, {
								label: "rgb res",
								value: state.rgb_resolution ?? "—"
							}),
							/* @__PURE__ */ jsx(Field, {
								label: "depth",
								value: state.depth_enabled ? "on" : "off"
							}),
							/* @__PURE__ */ jsx(Field, {
								label: "fps",
								value: state.observed_fps != null ? state.observed_fps.toFixed(1) : "—"
							})
						]
					}),
					state.error && /* @__PURE__ */ jsx("div", {
						className: "mt-2 rounded border border-rose-800 bg-rose-950/40 px-2 py-1 font-mono text-[10px] text-rose-200 whitespace-pre-wrap",
						children: state.error
					}),
					/* @__PURE__ */ jsxs("div", {
						className: "mt-2 flex items-center gap-2",
						children: [
							/* @__PURE__ */ jsxs("label", {
								className: "flex items-center gap-1.5 font-mono text-[10px] text-slate-400",
								onPointerDown: (e) => e.stopPropagation(),
								children: [/* @__PURE__ */ jsx("input", {
									type: "checkbox",
									checked: !!state.autoreconnect,
									onChange: (e) => onToggleAutoConnect(e.target.checked),
									disabled: !serviceRunning,
									className: "nodrag nopan accent-emerald-500"
								}), "auto-reconnect"]
							}),
							/* @__PURE__ */ jsx("button", {
								type: "button",
								onClick: onConnect,
								onPointerDown: (e) => e.stopPropagation(),
								disabled: !serviceRunning,
								className: "nodrag nopan rounded bg-sky-600 px-2 py-1 text-[10px] font-medium text-white hover:bg-sky-500 disabled:cursor-not-allowed disabled:opacity-40",
								children: connected ? "Reconnect" : "Connect"
							}),
							/* @__PURE__ */ jsx("button", {
								type: "button",
								onClick: onDisconnect,
								onPointerDown: (e) => e.stopPropagation(),
								disabled: !serviceRunning || !connected,
								className: "nodrag nopan rounded border border-slate-700 px-2 py-1 text-[10px] text-slate-300 hover:bg-slate-800 disabled:opacity-40",
								children: "Disconnect"
							}),
							/* @__PURE__ */ jsx("button", {
								type: "button",
								onClick: onSnapshot,
								onPointerDown: (e) => e.stopPropagation(),
								disabled: !serviceRunning || !connected,
								className: "nodrag nopan rounded border border-slate-700 px-2 py-1 text-[10px] text-slate-300 hover:bg-slate-800 disabled:opacity-40",
								children: "Snapshot"
							})
						]
					})
				]
			}),
			/* @__PURE__ */ jsxs("div", {
				className: "grid grid-cols-2 gap-2",
				children: [/* @__PURE__ */ jsx(FeedPanel, {
					label: "RGB",
					url: rgbUrl,
					connected
				}), /* @__PURE__ */ jsx(FeedPanel, {
					label: `Depth (${state.depth_enabled ? "on" : "off"})`,
					url: state.depth_enabled ? depthUrl : null,
					connected
				})]
			}),
			/* @__PURE__ */ jsxs("div", {
				className: "rounded border border-slate-800 bg-slate-900/40 p-2",
				children: [/* @__PURE__ */ jsxs("div", {
					className: "flex items-center gap-2",
					title: "Raw mono-camera feed, untouched by stereo matching — for confirming that camera's own view isn't blocked by a mount bracket, cable, or anything else on the robot. Left/right switch, not both at once — this robot's OAK-D Lite can't sustain both simultaneously on top of the existing RGB+depth load over USB2 (see the worke hardware doc).",
					children: [
						/* @__PURE__ */ jsx("span", {
							className: "text-[9px] uppercase tracking-wider text-slate-500",
							children: "mono camera"
						}),
						[
							"off",
							"left",
							"right"
						].map((mode) => /* @__PURE__ */ jsx("button", {
							type: "button",
							onClick: () => onSetMonoMode(mode),
							onPointerDown: (e) => e.stopPropagation(),
							disabled: !serviceRunning || !state.depth_enabled,
							className: `nodrag nopan rounded border px-2 py-1 text-[10px] capitalize disabled:cursor-not-allowed disabled:opacity-40 ${monoMode === mode ? "border-sky-500 bg-sky-950/60 text-sky-300" : "border-slate-700 text-slate-300 hover:bg-slate-800"}`,
							children: mode
						}, mode)),
						!state.depth_enabled && /* @__PURE__ */ jsx("span", {
							className: "text-[9px] text-slate-600",
							children: "needs depth enabled"
						})
					]
				}), monoMode !== "off" && /* @__PURE__ */ jsx("div", {
					className: "mt-2",
					children: /* @__PURE__ */ jsx(FeedPanel, {
						label: `Mono ${monoMode === "left" ? "Left" : "Right"}`,
						url: monoUrl,
						connected
					})
				})]
			}),
			/* @__PURE__ */ jsx(FilterPipeline, { proxyId }),
			snapshotOpen && /* @__PURE__ */ jsx("div", {
				className: "fixed inset-0 z-50 flex items-center justify-center bg-black/60",
				onClick: () => setSnapshotOpen(false),
				children: /* @__PURE__ */ jsxs("div", {
					className: "max-w-[80vw] rounded border border-slate-700 bg-slate-900 p-3",
					onClick: (e) => e.stopPropagation(),
					children: [
						/* @__PURE__ */ jsxs("div", {
							className: "mb-2 flex items-center justify-between gap-4",
							children: [/* @__PURE__ */ jsx("span", {
								className: "text-[11px] text-slate-300",
								children: "Snapshot"
							}), /* @__PURE__ */ jsx("button", {
								type: "button",
								onClick: () => setSnapshotOpen(false),
								className: "text-[11px] text-slate-500 hover:text-slate-200",
								children: "✕"
							})]
						}),
						snapshotPending && /* @__PURE__ */ jsx("div", {
							className: "text-[11px] text-slate-400",
							children: "waiting…"
						}),
						snapshotError && /* @__PURE__ */ jsx("div", {
							className: "text-[11px] text-rose-300",
							children: snapshotError
						}),
						snapshotPayload?.jpeg_b64 && /* @__PURE__ */ jsx("img", {
							alt: "snapshot",
							src: `data:image/jpeg;base64,${snapshotPayload.jpeg_b64}`,
							className: "max-h-[70vh] max-w-full"
						})
					]
				})
			})
		]
	});
}
function Field({ label, value, mono, hint, valueClass }) {
	return /* @__PURE__ */ jsxs("div", {
		className: "flex items-baseline gap-1",
		title: hint,
		children: [/* @__PURE__ */ jsx("span", {
			className: `text-[9px] uppercase tracking-wider text-slate-500 ${hint ? "cursor-help" : ""}`,
			children: label
		}), /* @__PURE__ */ jsx("span", {
			className: `text-[10px] ${mono ? "font-mono" : ""} ${valueClass ?? "text-slate-300"}`,
			children: value
		})]
	});
}
function FeedPanel({ label, url, connected }) {
	return /* @__PURE__ */ jsxs("div", {
		className: "rounded border border-slate-800 bg-slate-950/60",
		children: [/* @__PURE__ */ jsx("div", {
			className: "border-b border-slate-800 px-2 py-1 text-[9px] uppercase tracking-wider text-slate-500",
			children: label
		}), /* @__PURE__ */ jsx("div", {
			className: "flex aspect-video items-center justify-center overflow-hidden",
			children: url && connected ? /* @__PURE__ */ jsx("img", {
				src: url,
				alt: label,
				className: "max-h-full max-w-full"
			}) : /* @__PURE__ */ jsx("span", {
				className: "text-[10px] text-slate-600",
				children: connected ? "stream unavailable" : "not connected"
			})
		})]
	});
}
function FilterPipeline({ proxyId }) {
	const wsClient = useWsClient();
	const [catalog, setCatalog] = useState([]);
	const [filters, setFilters] = useState([]);
	const [expandedId, setExpandedId] = useState(null);
	const [addType, setAddType] = useState("");
	useEffect(() => {
		const offCat = wsClient.subscribe(`/oakd/${proxyId}/filter_catalog`, (f) => {
			if (f.method !== "message") return;
			const p = f.payload;
			if (p && Array.isArray(p.filters)) setCatalog(p.filters);
		});
		const offFil = wsClient.subscribe(`/oakd/${proxyId}/filters`, (f) => {
			if (f.method !== "message") return;
			const p = f.payload;
			if (p && Array.isArray(p.filters)) setFilters(p.filters);
		});
		return () => {
			offCat();
			offFil();
		};
	}, [proxyId, wsClient]);
	const sendControl = useCallback((action, args = {}) => {
		wsClient.publish(`/oakd/${proxyId}/control`, {
			action,
			...args
		});
	}, [wsClient, proxyId]);
	const onAdd = useCallback(() => {
		if (!addType) return;
		const id = typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `${addType}-${Date.now()}`;
		sendControl("add_filter", {
			type: addType,
			id
		});
		setExpandedId(id);
	}, [addType, sendControl]);
	const onRemove = useCallback((id) => {
		if (expandedId === id) setExpandedId(null);
		sendControl("remove_filter", { id });
	}, [sendControl, expandedId]);
	const onUpdate = useCallback((id, patch) => {
		sendControl("update_filter", {
			id,
			...patch
		});
	}, [sendControl]);
	const catalogByType = useMemo(() => new Map(catalog.map((c) => [c.type, c])), [catalog]);
	return /* @__PURE__ */ jsxs("section", {
		className: "rounded border border-slate-800 bg-slate-900/40",
		children: [/* @__PURE__ */ jsxs("header", {
			className: "flex items-center justify-between border-b border-slate-800 px-3 py-1.5",
			children: [/* @__PURE__ */ jsxs("div", {
				className: "flex items-baseline gap-2",
				children: [/* @__PURE__ */ jsx("h3", {
					className: "text-[10px] uppercase tracking-wider text-slate-400",
					children: "Filters (RGB)"
				}), /* @__PURE__ */ jsx("span", {
					className: "font-mono text-[10px] text-slate-500",
					children: filters.length
				})]
			}), /* @__PURE__ */ jsxs("div", {
				className: "flex items-center gap-1",
				children: [/* @__PURE__ */ jsxs("select", {
					value: addType,
					onChange: (e) => setAddType(e.target.value),
					onPointerDown: (e) => e.stopPropagation(),
					className: "nodrag nopan rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 text-[10px]",
					children: [/* @__PURE__ */ jsx("option", {
						value: "",
						children: "add filter…"
					}), catalog.map((c) => /* @__PURE__ */ jsx("option", {
						value: c.type,
						children: c.title
					}, c.type))]
				}), /* @__PURE__ */ jsx("button", {
					type: "button",
					onClick: onAdd,
					onPointerDown: (e) => e.stopPropagation(),
					disabled: !addType,
					className: "nodrag nopan rounded bg-sky-700 px-2 py-0.5 text-[10px] text-white hover:bg-sky-600 disabled:opacity-40",
					children: "Add"
				})]
			})]
		}), /* @__PURE__ */ jsxs("div", {
			className: "flex flex-col gap-1 p-2",
			children: [filters.length === 0 && /* @__PURE__ */ jsx("div", {
				className: "text-[10px] text-slate-600",
				children: "no filters — RGB stream is unmodified"
			}), filters.map((spec) => {
				const cat = catalogByType.get(spec.type);
				const expanded = expandedId === spec.id;
				return /* @__PURE__ */ jsxs("div", {
					className: "rounded border border-slate-800 bg-slate-950/40",
					children: [/* @__PURE__ */ jsxs("div", {
						className: "flex items-center justify-between gap-2 px-2 py-1",
						children: [
							/* @__PURE__ */ jsxs("button", {
								type: "button",
								onClick: () => setExpandedId(expanded ? null : spec.id),
								onPointerDown: (e) => e.stopPropagation(),
								className: "nodrag nopan flex-1 truncate text-left text-[11px] text-slate-200",
								children: [
									expanded ? "▾" : "▸",
									" ",
									spec.title ?? cat?.title ?? spec.type
								]
							}),
							/* @__PURE__ */ jsx("button", {
								type: "button",
								onClick: () => onUpdate(spec.id, { enabled: !spec.enabled }),
								onPointerDown: (e) => e.stopPropagation(),
								className: `nodrag nopan rounded px-1.5 py-0.5 text-[9px] ${spec.enabled ? "bg-emerald-900/50 text-emerald-300" : "bg-slate-800 text-slate-500"}`,
								children: spec.enabled ? "ON" : "OFF"
							}),
							/* @__PURE__ */ jsx("button", {
								type: "button",
								onClick: () => onRemove(spec.id),
								onPointerDown: (e) => e.stopPropagation(),
								className: "nodrag nopan text-[11px] text-slate-500 hover:text-rose-400",
								children: "✕"
							})
						]
					}), expanded && cat && /* @__PURE__ */ jsx("div", {
						className: "flex flex-col gap-1.5 border-t border-slate-800 p-2",
						children: cat.param_schema.map((schema) => /* @__PURE__ */ jsx(ParamInput, {
							schema,
							value: spec.params[schema.name],
							onChange: (v) => onUpdate(spec.id, { params: { [schema.name]: v } })
						}, schema.name))
					})]
				}, spec.id);
			})]
		})]
	});
}
function ParamInput({ schema, value, onChange }) {
	const label = schema.label ?? schema.name;
	const current = value === void 0 ? schema.default : value;
	if (schema.type === "point" || schema.type === "rect" || schema.type === "points") return /* @__PURE__ */ jsxs("label", {
		className: "flex items-center gap-2",
		children: [/* @__PURE__ */ jsx("span", {
			className: "w-24 text-[10px] uppercase tracking-wider text-slate-500",
			children: label
		}), /* @__PURE__ */ jsx("input", {
			type: "text",
			defaultValue: JSON.stringify(current ?? []),
			onBlur: (e) => {
				try {
					onChange(JSON.parse(e.target.value));
				} catch {}
			},
			onPointerDown: (e) => e.stopPropagation(),
			className: "nodrag nopan flex-1 rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 font-mono text-[10px]"
		})]
	});
	if (schema.type === "bool") return /* @__PURE__ */ jsxs("label", {
		className: "flex items-center gap-2",
		children: [/* @__PURE__ */ jsx("input", {
			type: "checkbox",
			checked: !!current,
			onChange: (e) => onChange(e.target.checked),
			onPointerDown: (e) => e.stopPropagation(),
			className: "nodrag nopan"
		}), /* @__PURE__ */ jsx("span", {
			className: "text-[11px] text-slate-300",
			children: label
		})]
	});
	if (schema.type === "enum") return /* @__PURE__ */ jsxs("label", {
		className: "flex items-center gap-2",
		children: [/* @__PURE__ */ jsx("span", {
			className: "w-24 text-[10px] uppercase tracking-wider text-slate-500",
			children: label
		}), /* @__PURE__ */ jsx("select", {
			value: String(current),
			onChange: (e) => onChange(e.target.value),
			onPointerDown: (e) => e.stopPropagation(),
			className: "nodrag nopan rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 text-[11px]",
			children: (schema.choices ?? []).map((c) => /* @__PURE__ */ jsx("option", {
				value: c,
				children: c
			}, c))
		})]
	});
	if (schema.type === "string") return /* @__PURE__ */ jsxs("label", {
		className: "flex items-center gap-2",
		children: [/* @__PURE__ */ jsx("span", {
			className: "w-24 shrink-0 text-[10px] uppercase tracking-wider text-slate-500",
			children: label
		}), /* @__PURE__ */ jsx("input", {
			type: "text",
			value: typeof current === "string" ? current : "",
			onChange: (e) => onChange(e.target.value),
			onPointerDown: (e) => e.stopPropagation(),
			placeholder: schema.placeholder,
			className: "nodrag nopan flex-1 rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 font-mono text-[11px]"
		})]
	});
	const num = typeof current === "number" ? current : Number(current) || 0;
	return /* @__PURE__ */ jsxs("div", {
		className: "flex items-center gap-2",
		children: [
			/* @__PURE__ */ jsx("span", {
				className: "w-24 text-[10px] uppercase tracking-wider text-slate-500",
				children: label
			}),
			schema.min != null && schema.max != null && /* @__PURE__ */ jsx("input", {
				type: "range",
				min: schema.min,
				max: schema.max,
				step: schema.step ?? (schema.type === "int" ? 1 : .01),
				value: num,
				onChange: (e) => onChange(schema.type === "int" ? parseInt(e.target.value, 10) : parseFloat(e.target.value)),
				onPointerDown: (e) => e.stopPropagation(),
				className: "nodrag nopan flex-1"
			}),
			/* @__PURE__ */ jsx("input", {
				type: "number",
				min: schema.min,
				max: schema.max,
				step: schema.step ?? (schema.type === "int" ? 1 : .01),
				value: num,
				onChange: (e) => {
					const v = e.target.value;
					if (v !== "") onChange(schema.type === "int" ? parseInt(v, 10) : parseFloat(v));
				},
				onPointerDown: (e) => e.stopPropagation(),
				className: "nodrag nopan w-16 rounded border border-slate-700 bg-slate-950 px-1.5 py-0.5 text-right font-mono text-[11px]"
			})
		]
	});
}
//#endregion
export { OakdFullView as default };
