// GENERATED FILE — built by build-service-ui.mjs from View.tsx. Do not edit; do not lint.
/* eslint-disable */
// @ts-nocheck
import { useCallback, useEffect, useState } from "react";
import { useActiveRuntime, useWsClient } from "@rlx/ui";
import { Fragment, jsx, jsxs } from "react/jsx-runtime";
//#region ../botgear/repo/runtime/1.0.0/ui/View.tsx
function fmtBytes(n) {
	if (n == null) return "—";
	const units = [
		"B",
		"KiB",
		"MiB",
		"GiB",
		"TiB"
	];
	let v = n;
	let i = 0;
	while (v >= 1024 && i < units.length - 1) {
		v /= 1024;
		i++;
	}
	return `${v.toFixed(v < 10 ? 2 : 1)} ${units[i]}`;
}
function fmtUptime(seconds) {
	if (seconds == null) return "—";
	const s = Math.floor(seconds);
	const d = Math.floor(s / 86400);
	const h = Math.floor(s % 86400 / 3600);
	const m = Math.floor(s % 3600 / 60);
	const sec = s % 60;
	if (d > 0) return `${d}d ${h}h ${m}m`;
	if (h > 0) return `${h}h ${m}m`;
	if (m > 0) return `${m}m ${sec}s`;
	return `${sec}s`;
}
function fmtPct(n) {
	return n == null ? "—" : `${n.toFixed(1)}%`;
}
function fmtDuration(seconds) {
	if (seconds == null) return "—";
	const s = Math.max(0, Math.floor(seconds));
	const h = Math.floor(s / 3600);
	const m = Math.floor(s % 3600 / 60);
	if (h > 0) return `${h}h ${m}m`;
	return `${m}m`;
}
function fmtTimestamp(ts) {
	if (ts == null) return "—";
	return (/* @__PURE__ */ new Date(ts * 1e3)).toLocaleTimeString();
}
function RuntimeFullView({ proxy }) {
	const wsClient = useWsClient();
	const proxyId = proxy.id ?? proxy.name ?? "";
	const stateTopic = `/runtime/${proxyId}/state`;
	const powerTopic = `/runtime/${proxyId}/power`;
	const [state, setState] = useState({});
	const [power, setPower] = useState({});
	const [lastUpdate, setLastUpdate] = useState(null);
	const { connection, runtimeId } = useActiveRuntime();
	const [federationId, setFederationId] = useState(connection?.meta.runtime_id ?? null);
	useEffect(() => {
		if (!connection) return;
		const update = () => setFederationId(connection.meta.runtime_id ?? null);
		update();
		return connection.subscribe(update);
	}, [connection]);
	const copyId = useCallback((text) => {
		try {
			navigator.clipboard?.writeText(text);
		} catch {}
	}, []);
	useEffect(() => {
		if (!proxyId) return;
		return wsClient.subscribe(stateTopic, (f) => {
			if (f.method !== "message") return;
			const p = f.payload;
			if (!p || typeof p !== "object") return;
			setState(p);
			setLastUpdate(Date.now());
		});
	}, [
		proxyId,
		stateTopic,
		wsClient
	]);
	useEffect(() => {
		if (!proxyId) return;
		return wsClient.subscribe(powerTopic, (f) => {
			if (f.method !== "message") return;
			const p = f.payload;
			if (!p || typeof p !== "object") return;
			setPower(p);
		});
	}, [
		proxyId,
		powerTopic,
		wsClient
	]);
	const proc = state.process ?? {};
	const os = state.os ?? {};
	const cpu = state.cpu ?? {};
	const mem = state.memory ?? {};
	const disk = state.disk ?? {};
	const battery = power.battery ?? null;
	const ac = power.ac ?? null;
	const suspend = power.suspend ?? {};
	const langLabel = state.language_version ? `${state.language ?? "python"} ${state.language_version}` : state.language ?? "python";
	return /* @__PURE__ */ jsxs("div", {
		className: "flex min-w-[420px] flex-col gap-3 p-3 text-xs",
		children: [
			/* @__PURE__ */ jsxs("section", {
				className: "rounded border border-magenta-700 bg-slate-900/60 p-3",
				children: [/* @__PURE__ */ jsxs("div", {
					className: "mb-1 flex items-baseline justify-between gap-2",
					children: [/* @__PURE__ */ jsx("span", {
						className: "text-[9px] uppercase tracking-wider text-slate-500",
						children: "federation id"
					}), federationId ? /* @__PURE__ */ jsx("button", {
						type: "button",
						onClick: (e) => {
							e.stopPropagation();
							copyId(federationId);
						},
						onPointerDown: (e) => e.stopPropagation(),
						title: "Copy id to clipboard",
						className: "nodrag nopan rounded px-1.5 py-0.5 text-[9px] text-slate-500 hover:bg-slate-800 hover:text-slate-200",
						children: "copy"
					}) : null]
				}), federationId ? /* @__PURE__ */ jsxs(Fragment, { children: [/* @__PURE__ */ jsx("div", {
					className: "select-all font-mono text-base font-semibold text-fuchsia-300",
					title: federationId,
					children: federationId
				}), /* @__PURE__ */ jsxs("div", {
					className: "mt-1 font-mono text-[10px] text-slate-500",
					children: [
						"Peers address services on this runtime via the",
						" ",
						/* @__PURE__ */ jsxs("span", {
							className: "text-fuchsia-400",
							children: ["@", federationId]
						}),
						" suffix —",
						" ",
						"e.g. ",
						/* @__PURE__ */ jsxs("span", {
							className: "text-cyan-400",
							children: ["/clock/clock-1@", federationId]
						})
					]
				})] }) : /* @__PURE__ */ jsxs("div", {
					className: "font-mono text-[10px] text-amber-300",
					children: [
						"(no federation id yet — waiting for /runtime/info; chip-label is",
						" ",
						/* @__PURE__ */ jsx("span", {
							className: "text-slate-400",
							children: runtimeId
						}),
						")"
					]
				})]
			}),
			lastUpdate === null && /* @__PURE__ */ jsxs("div", {
				className: "rounded border border-amber-700 bg-amber-950/40 px-2 py-1 font-mono text-[10px] text-amber-200",
				children: [
					"waiting for first /runtime/",
					proxyId,
					"/state frame…"
				]
			}),
			/* @__PURE__ */ jsxs(Section, {
				title: "process",
				children: [
					/* @__PURE__ */ jsxs(Grid, { children: [
						/* @__PURE__ */ jsx(Field, {
							label: "language",
							value: langLabel,
							mono: true
						}),
						/* @__PURE__ */ jsx(Field, {
							label: "pid",
							value: proc.pid?.toString() ?? "—",
							mono: true
						}),
						/* @__PURE__ */ jsx(Field, {
							label: "uptime",
							value: fmtUptime(proc.uptime_s)
						}),
						/* @__PURE__ */ jsx(Field, {
							label: "threads",
							value: proc.threads?.toString() ?? "—"
						}),
						/* @__PURE__ */ jsx(Field, {
							label: "rss",
							value: fmtBytes(proc.rss_bytes)
						}),
						/* @__PURE__ */ jsx(Field, {
							label: "cpu",
							value: fmtPct(proc.cpu_percent)
						})
					] }),
					proc.cmdline && /* @__PURE__ */ jsxs("div", {
						className: "mt-2 truncate font-mono text-[10px] text-slate-500",
						title: proc.cmdline,
						children: ["$ ", proc.cmdline]
					}),
					proc.cwd && /* @__PURE__ */ jsxs("div", {
						className: "truncate font-mono text-[10px] text-slate-500",
						title: proc.cwd,
						children: ["cwd: ", proc.cwd]
					})
				]
			}),
			/* @__PURE__ */ jsxs(Section, {
				title: "operating system",
				children: [/* @__PURE__ */ jsxs(Grid, { children: [
					/* @__PURE__ */ jsx(Field, {
						label: "name",
						value: os.name ?? "—"
					}),
					/* @__PURE__ */ jsx(Field, {
						label: "version",
						value: os.version ?? "—"
					}),
					/* @__PURE__ */ jsx(Field, {
						label: "arch",
						value: os.arch ?? "—",
						mono: true
					}),
					/* @__PURE__ */ jsx(Field, {
						label: "hostname",
						value: os.hostname ?? "—",
						mono: true
					})
				] }), os.kernel && os.kernel !== os.version && /* @__PURE__ */ jsxs("div", {
					className: "mt-1 truncate font-mono text-[10px] text-slate-500",
					title: os.kernel,
					children: ["kernel: ", os.kernel]
				})]
			}),
			(battery || ac) && /* @__PURE__ */ jsxs(Section, {
				title: "power",
				children: [
					battery && /* @__PURE__ */ jsxs(Fragment, { children: [/* @__PURE__ */ jsxs(Grid, { children: [
						/* @__PURE__ */ jsx(Field, {
							label: "battery",
							value: `${battery.percent != null ? battery.percent.toFixed(0) + "%" : "—"} · ${battery.status ?? "unknown"}`,
							hint: "Current charge level and charging state, read from the OS battery driver (Linux: /sys/class/power_supply)."
						}),
						/* @__PURE__ */ jsx(Field, {
							label: battery.status === "charging" ? "time to full" : "time to empty",
							value: battery.status === "charging" ? fmtDuration(battery.secs_to_full) : fmtDuration(battery.secs_to_empty),
							hint: "Estimate from the current charge/discharge rate. Only available while actively charging or discharging — blank when idle, full, or on AC with no load."
						}),
						/* @__PURE__ */ jsx(Field, {
							label: "health",
							value: battery.health_percent != null ? `${battery.health_percent.toFixed(0)}%` : "—",
							hint: "The battery's current maximum charge capacity vs. its original design capacity. Naturally drops below 100% with age/charge cycles — this is expected wear, not a fault. Below ~80% is typically when people consider replacing a battery."
						}),
						/* @__PURE__ */ jsx(Field, {
							label: "cycles",
							value: battery.cycle_count?.toString() ?? "—",
							hint: "Full charge/discharge cycles, as counted by the battery's embedded controller. Many laptops' controllers don't implement this counter and always report 0 regardless of actual use — that's a hardware/firmware limitation of this reading, not a sign the battery is unused."
						})
					] }), /* @__PURE__ */ jsx(Bar, {
						percent: battery.percent ?? void 0,
						sense: "level"
					})] }),
					/* @__PURE__ */ jsxs("div", {
						className: "mt-2 flex flex-wrap items-center gap-x-4 gap-y-1",
						children: [/* @__PURE__ */ jsx(Field, {
							label: "ac power",
							value: ac?.online ? "plugged in" : ac ? "unplugged" : "—",
							hint: "Whether an AC/mains adapter is currently connected and supplying power."
						}), /* @__PURE__ */ jsx(Field, {
							label: "suspend/resume",
							value: !suspend.supported ? "not monitored" : suspend.last_event ? `last: ${suspend.last_event} @ ${fmtTimestamp(suspend.last_event_ts)}` : "monitored, none yet",
							hint: "Whether this runtime is watching the OS for suspend/resume (Linux: systemd-logind's PrepareForSleep signal), and the most recent transition observed since this backend started."
						})]
					}),
					battery?.status === "discharging" && !ac?.online && /* @__PURE__ */ jsxs("div", {
						className: "mt-2 rounded border border-amber-700 bg-amber-950/40 px-2 py-1 font-mono text-[10px] text-amber-200",
						children: [
							"running on battery — ",
							fmtDuration(battery.secs_to_empty),
							" remaining"
						]
					})
				]
			}),
			/* @__PURE__ */ jsxs(Section, {
				title: "cpu",
				children: [/* @__PURE__ */ jsxs(Grid, { children: [
					/* @__PURE__ */ jsx(Field, {
						label: "cores",
						value: `${cpu.logical ?? "—"} logical${cpu.physical != null ? ` · ${cpu.physical} physical` : ""}`
					}),
					/* @__PURE__ */ jsx(Field, {
						label: "usage",
						value: fmtPct(cpu.percent)
					}),
					/* @__PURE__ */ jsx(Field, {
						label: "load avg",
						value: cpu.load_avg_1 == null ? "—" : `${cpu.load_avg_1.toFixed(2)} · ${cpu.load_avg_5?.toFixed(2) ?? "—"} · ${cpu.load_avg_15?.toFixed(2) ?? "—"}`,
						mono: true
					})
				] }), /* @__PURE__ */ jsx(Bar, { percent: cpu.percent })]
			}),
			/* @__PURE__ */ jsxs(Section, {
				title: "memory",
				children: [/* @__PURE__ */ jsxs(Grid, { children: [
					/* @__PURE__ */ jsx(Field, {
						label: "total",
						value: fmtBytes(mem.total_bytes)
					}),
					/* @__PURE__ */ jsx(Field, {
						label: "used",
						value: fmtBytes(mem.used_bytes)
					}),
					/* @__PURE__ */ jsx(Field, {
						label: "available",
						value: fmtBytes(mem.available_bytes)
					}),
					/* @__PURE__ */ jsx(Field, {
						label: "usage",
						value: fmtPct(mem.percent)
					})
				] }), /* @__PURE__ */ jsx(Bar, { percent: mem.percent })]
			}),
			/* @__PURE__ */ jsx(Section, {
				title: `disk (${disk.mount ?? "/"})`,
				children: disk.error ? /* @__PURE__ */ jsx("div", {
					className: "font-mono text-[10px] text-rose-300",
					children: disk.error
				}) : /* @__PURE__ */ jsxs(Fragment, { children: [/* @__PURE__ */ jsxs(Grid, { children: [
					/* @__PURE__ */ jsx(Field, {
						label: "total",
						value: fmtBytes(disk.total_bytes)
					}),
					/* @__PURE__ */ jsx(Field, {
						label: "used",
						value: fmtBytes(disk.used_bytes)
					}),
					/* @__PURE__ */ jsx(Field, {
						label: "free",
						value: fmtBytes(disk.free_bytes)
					}),
					/* @__PURE__ */ jsx(Field, {
						label: "usage",
						value: fmtPct(disk.percent)
					})
				] }), /* @__PURE__ */ jsx(Bar, { percent: disk.percent })] })
			})
		]
	});
}
function Section({ title, children }) {
	return /* @__PURE__ */ jsxs("section", {
		className: "rounded border border-slate-800 bg-slate-900/40 p-3",
		children: [/* @__PURE__ */ jsx("h3", {
			className: "mb-2 text-[10px] font-semibold uppercase tracking-wider text-slate-400",
			children: title
		}), children]
	});
}
function Grid({ children }) {
	return /* @__PURE__ */ jsx("div", {
		className: "grid grid-cols-2 gap-x-3 gap-y-1.5",
		children
	});
}
function Field({ label, value, mono, hint }) {
	return /* @__PURE__ */ jsxs("div", {
		className: "flex items-baseline justify-between gap-2",
		title: hint,
		children: [/* @__PURE__ */ jsx("span", {
			className: `text-[10px] uppercase tracking-wider text-slate-500 ${hint ? "cursor-help decoration-dotted underline-offset-2 hover:underline" : ""}`,
			children: label
		}), /* @__PURE__ */ jsx("span", {
			className: `truncate text-slate-200 ${mono ? "font-mono text-[11px]" : ""}`,
			title: hint ?? value,
			children: value
		})]
	});
}
/** Horizontal usage/level bar.
*
* Two color modes, chosen by `sense`:
*  - 'usage' (default) — for things where HIGH is bad (CPU/mem/disk load):
*    amber → rose as percent climbs.
*  - 'level' — for things where HIGH is good (battery charge): rose only
*    when critically low, amber when getting low, green otherwise. Using
*    'usage' coloring on a battery bar would paint a full battery red,
*    which reads backwards.
*/
function Bar({ percent, sense = "usage" }) {
	if (percent == null) return null;
	const pct = Math.max(0, Math.min(100, percent));
	return /* @__PURE__ */ jsx("div", {
		className: "mt-2 h-1.5 w-full overflow-hidden rounded bg-slate-800",
		children: /* @__PURE__ */ jsx("div", {
			className: `h-full ${sense === "level" ? pct <= 15 ? "bg-rose-500" : pct <= 30 ? "bg-amber-500" : "bg-emerald-500" : pct >= 90 ? "bg-rose-500" : pct >= 75 ? "bg-amber-500" : "bg-emerald-500"}`,
			style: { width: `${pct}%` }
		})
	});
}
//#endregion
export { RuntimeFullView as default };
