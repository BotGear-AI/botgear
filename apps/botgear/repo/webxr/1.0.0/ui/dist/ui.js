// GENERATED FILE — built by build-service-ui.mjs from View.tsx. Do not edit; do not lint.
/* eslint-disable */
// @ts-nocheck
import { createContext, createElement, forwardRef, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { useServiceRequest, useWsClient } from "@rlx/ui";
import { Fragment, jsx, jsxs } from "react/jsx-runtime";
//#region ../botgear/repo/webxr/1.0.0/ui/node_modules/lucide-react/dist/esm/shared/src/utils/toKebabCase.mjs
/**
* @license lucide-react v1.45.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
var toKebabCase = (string) => string?.replace(/([a-z0-9])([A-Z])/g, "$1-$2").toLowerCase();
//#endregion
//#region ../botgear/repo/webxr/1.0.0/ui/node_modules/lucide-react/dist/esm/shared/src/utils/toLucideIconData.mjs
/**
* @license lucide-react v1.45.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
function toLucideIconData(iconName, iconNode, aliases = []) {
	if (iconNode == null) throw new Error("[lucide]: iconNode is required when icon name is used");
	return {
		name: toKebabCase(iconName),
		size: 24,
		node: iconNode,
		...aliases.length > 0 ? { aliases } : {}
	};
}
//#endregion
//#region ../botgear/repo/webxr/1.0.0/ui/node_modules/lucide-react/dist/esm/shared/src/utils/toCamelCase.mjs
/**
* @license lucide-react v1.45.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
var toCamelCase = (string) => {
	let out = "";
	let upperNext = false;
	for (const ch of string) {
		if (ch === "-" || ch === "_" || ch <= " ") {
			upperNext = out.length > 0;
			continue;
		}
		if (out.length === 0) out += ch.toLowerCase();
		else out += upperNext ? ch.toUpperCase() : ch;
		upperNext = false;
	}
	return out;
};
//#endregion
//#region ../botgear/repo/webxr/1.0.0/ui/node_modules/lucide-react/dist/esm/shared/src/utils/toPascalCase.mjs
/**
* @license lucide-react v1.45.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
var toPascalCase = (string) => {
	const camelCase = toCamelCase(string);
	return camelCase.charAt(0).toUpperCase() + camelCase.slice(1);
};
//#endregion
//#region ../botgear/repo/webxr/1.0.0/ui/node_modules/lucide-react/dist/esm/shared/src/utils/mergeClasses.mjs
/**
* @license lucide-react v1.45.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
var mergeClasses = (...classes) => classes.filter((className, index, array) => {
	return Boolean(className) && className.trim() !== "" && array.indexOf(className) === index;
}).join(" ").trim();
//#endregion
//#region ../botgear/repo/webxr/1.0.0/ui/node_modules/lucide-react/dist/esm/shared/src/build/defaultAttributes.mjs
/**
* @license lucide-react v1.45.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
var defaultAttributes = {
	xmlns: "http://www.w3.org/2000/svg",
	width: 24,
	height: 24,
	viewBox: "0 0 24 24",
	fill: "none",
	stroke: "currentColor",
	"stroke-width": 2,
	"stroke-linecap": "round",
	"stroke-linejoin": "round"
};
//#endregion
//#region ../botgear/repo/webxr/1.0.0/ui/node_modules/lucide-react/dist/esm/shared/src/build/buildLucideIconNode.mjs
/**
* @license lucide-react v1.45.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
function isDefined(value) {
	return value !== null && value !== void 0;
}
function buildLucideIconNode(icon, params = {}) {
	const attributeNames = params.attributeNames ?? {};
	const getAttributeName = (attributeName) => attributeNames[attributeName] ?? attributeName;
	const viewBoxWidth = icon.size ?? icon.width ?? defaultAttributes["width"];
	const viewBoxHeight = icon.size ?? icon.height ?? defaultAttributes["height"];
	const aliasClassNames = icon.aliases?.filter((alias) => typeof alias === "string" && alias.trim() !== "").map((alias) => `lucide-${alias}`) ?? [];
	const iconClassNames = [...icon.name ? [`lucide-${icon.name}`] : [], ...aliasClassNames];
	const classNamesFromClassName = params.className?.split(" ").filter(Boolean) ?? [];
	const className = params.includeDefaultClasses === false ? mergeClasses(...classNamesFromClassName) : mergeClasses("lucide", ...iconClassNames, ...classNamesFromClassName);
	const calculatedStrokeWidth = params.absoluteStrokeWidth ? Number(params.strokeWidth ?? defaultAttributes["stroke-width"]) * Number(icon.size ?? icon.width ?? defaultAttributes["width"]) / Number(params.size ?? params.width ?? defaultAttributes["width"]) : params.strokeWidth ?? defaultAttributes["stroke-width"];
	return [
		"svg",
		{
			...Object.entries(defaultAttributes).reduce((attrs, [attrName, value]) => {
				attrs[getAttributeName(attrName)] = value;
				return attrs;
			}, {}),
			..."color" in params && params.color && { [getAttributeName("stroke")]: params.color },
			..."size" in params && isDefined(params.size) && {
				[getAttributeName("width")]: params.size,
				[getAttributeName("height")]: params.size
			},
			..."width" in params && isDefined(params.width) && { [getAttributeName("width")]: params.width },
			..."height" in params && isDefined(params.height) && { [getAttributeName("height")]: params.height },
			[getAttributeName("stroke-width")]: calculatedStrokeWidth,
			...className && { [getAttributeName("class")]: className },
			[getAttributeName("viewBox")]: `0 0 ${viewBoxWidth} ${viewBoxHeight}`,
			...params.hasA11yProp === false ? { [getAttributeName("aria-hidden")]: "true" } : {},
			..."attributes" in params && params.attributes
		},
		icon.node.map((child) => {
			const [name, attrs, children] = child;
			const nextAttrs = params.nonScalingStroke ? {
				[getAttributeName("vector-effect")]: "non-scaling-stroke",
				...attrs
			} : attrs;
			return children ? [
				name,
				nextAttrs,
				children
			] : [name, nextAttrs];
		})
	];
}
//#endregion
//#region ../botgear/repo/webxr/1.0.0/ui/node_modules/lucide-react/dist/esm/shared/src/build/buildLucideIconForReact.mjs
/**
* @license lucide-react v1.45.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
function buildLucideIconForReact(icon, params = {}) {
	return buildLucideIconNode(icon, {
		...params,
		attributeNames: {
			...params.attributeNames,
			class: "className",
			"stroke-width": "strokeWidth",
			"stroke-linecap": "strokeLinecap",
			"stroke-linejoin": "strokeLinejoin",
			"vector-effect": "vectorEffect"
		}
	});
}
//#endregion
//#region ../botgear/repo/webxr/1.0.0/ui/node_modules/lucide-react/dist/esm/shared/src/utils/hasA11yProp.mjs
/**
* @license lucide-react v1.45.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
var hasA11yProp = (props) => {
	for (const prop in props) if (prop.startsWith("aria-") || prop === "role" || prop === "title") return true;
	return false;
};
//#endregion
//#region ../botgear/repo/webxr/1.0.0/ui/node_modules/lucide-react/dist/esm/context.mjs
/**
* @license lucide-react v1.45.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
var LucideContext = createContext({});
var useLucideContext = () => useContext(LucideContext);
//#endregion
//#region ../botgear/repo/webxr/1.0.0/ui/node_modules/lucide-react/dist/esm/Icon.mjs
/**
* @license lucide-react v1.45.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
var Icon = forwardRef(({ color, size, width, height, strokeWidth, absoluteStrokeWidth, nonScalingStroke, className = "", children, iconNode = [], icon = {
	node: iconNode,
	aliases: [],
	size: 24
}, ...rest }, ref) => {
	const { size: contextSize = 24, strokeWidth: contextStrokeWidth = 2, absoluteStrokeWidth: contextAbsoluteStrokeWidth = false, nonScalingStroke: contextNonScalingStroke = false, color: contextColor = "currentColor", className: contextClass = "" } = useLucideContext() ?? {};
	const hasAccessibleProp = Boolean(children) || hasA11yProp(rest);
	const [name, svgAttributes, builtIconNode = []] = buildLucideIconForReact(icon, {
		color: color ?? contextColor,
		width: width ?? size ?? contextSize,
		height: height ?? size ?? contextSize,
		strokeWidth: strokeWidth ?? contextStrokeWidth,
		absoluteStrokeWidth: absoluteStrokeWidth ?? contextAbsoluteStrokeWidth,
		nonScalingStroke: nonScalingStroke ?? contextNonScalingStroke,
		className: mergeClasses(contextClass, className),
		hasA11yProp: hasAccessibleProp,
		attributes: rest
	});
	return createElement(name, {
		ref,
		...svgAttributes
	}, [...builtIconNode.map(([tag, attrs]) => createElement(tag, attrs)), ...Array.isArray(children) ? children : [children]]);
});
//#endregion
//#region ../botgear/repo/webxr/1.0.0/ui/node_modules/lucide-react/dist/esm/createLucideIcon.mjs
/**
* @license lucide-react v1.45.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
function createLucideIcon(iconDataOrName, iconNode = [], aliases = []) {
	const iconData = typeof iconDataOrName === "string" ? toLucideIconData(iconDataOrName, iconNode, aliases) : iconDataOrName;
	const Component = forwardRef(({ className, ...props }, ref) => createElement(Icon, {
		ref,
		icon: iconData,
		className,
		...props
	}));
	if (iconData.name) Component.displayName = toPascalCase(iconData.name);
	return Component;
}
//#endregion
//#region ../botgear/repo/webxr/1.0.0/ui/node_modules/lucide-react/dist/esm/icons/crosshair.mjs
/**
* @license lucide-react v1.45.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
var __iconData$6 = {
	name: "crosshair",
	size: 24,
	node: [
		["circle", {
			cx: "12",
			cy: "12",
			r: "10",
			key: "1mglay"
		}],
		["line", {
			x1: "22",
			x2: "18",
			y1: "12",
			y2: "12",
			key: "l9bcsi"
		}],
		["line", {
			x1: "6",
			x2: "2",
			y1: "12",
			y2: "12",
			key: "13hhkx"
		}],
		["line", {
			x1: "12",
			x2: "12",
			y1: "6",
			y2: "2",
			key: "10w3f3"
		}],
		["line", {
			x1: "12",
			x2: "12",
			y1: "22",
			y2: "18",
			key: "15g9kq"
		}]
	]
};
__iconData$6.node;
var Crosshair = createLucideIcon(__iconData$6);
//#endregion
//#region ../botgear/repo/webxr/1.0.0/ui/node_modules/lucide-react/dist/esm/icons/eye.mjs
/**
* @license lucide-react v1.45.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
var __iconData$5 = {
	name: "eye",
	size: 24,
	node: [["path", {
		d: "M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0",
		key: "1nclc0"
	}], ["circle", {
		cx: "12",
		cy: "12",
		r: "3",
		key: "1v7zrd"
	}]]
};
__iconData$5.node;
var Eye = createLucideIcon(__iconData$5);
//#endregion
//#region ../botgear/repo/webxr/1.0.0/ui/node_modules/lucide-react/dist/esm/icons/gamepad-2.mjs
/**
* @license lucide-react v1.45.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
var __iconData$4 = {
	name: "gamepad-2",
	size: 24,
	node: [
		["line", {
			x1: "6",
			x2: "10",
			y1: "11",
			y2: "11",
			key: "1gktln"
		}],
		["line", {
			x1: "8",
			x2: "8",
			y1: "9",
			y2: "13",
			key: "qnk9ow"
		}],
		["line", {
			x1: "15",
			x2: "15.01",
			y1: "12",
			y2: "12",
			key: "krot7o"
		}],
		["line", {
			x1: "18",
			x2: "18.01",
			y1: "10",
			y2: "10",
			key: "1lcuu1"
		}],
		["path", {
			d: "M17.32 5H6.68a4 4 0 0 0-3.978 3.59c-.006.052-.01.101-.017.152C2.604 9.416 2 14.456 2 16a3 3 0 0 0 3 3c1 0 1.5-.5 2-1l1.414-1.414A2 2 0 0 1 9.828 16h4.344a2 2 0 0 1 1.414.586L17 18c.5.5 1 1 2 1a3 3 0 0 0 3-3c0-1.545-.604-6.584-.685-7.258-.007-.05-.011-.1-.017-.151A4 4 0 0 0 17.32 5z",
			key: "mfqc10"
		}]
	]
};
__iconData$4.node;
var Gamepad2 = createLucideIcon(__iconData$4);
//#endregion
//#region ../botgear/repo/webxr/1.0.0/ui/node_modules/lucide-react/dist/esm/icons/globe.mjs
/**
* @license lucide-react v1.45.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
var __iconData$3 = {
	name: "globe",
	size: 24,
	node: [
		["circle", {
			cx: "12",
			cy: "12",
			r: "10",
			key: "1mglay"
		}],
		["path", {
			d: "M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20",
			key: "13o1zl"
		}],
		["path", {
			d: "M2 12h20",
			key: "9i4pu4"
		}]
	]
};
__iconData$3.node;
var Globe = createLucideIcon(__iconData$3);
//#endregion
//#region ../botgear/repo/webxr/1.0.0/ui/node_modules/lucide-react/dist/esm/icons/plus.mjs
/**
* @license lucide-react v1.45.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
var __iconData$2 = {
	name: "plus",
	size: 24,
	node: [["path", {
		d: "M5 12h14",
		key: "1ays0h"
	}], ["path", {
		d: "M12 5v14",
		key: "s699le"
	}]]
};
__iconData$2.node;
var Plus = createLucideIcon(__iconData$2);
//#endregion
//#region ../botgear/repo/webxr/1.0.0/ui/node_modules/lucide-react/dist/esm/icons/trash.mjs
/**
* @license lucide-react v1.45.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
var __iconData$1 = {
	name: "trash",
	size: 24,
	node: [
		["path", {
			d: "M10 11v6",
			key: "nco0om"
		}],
		["path", {
			d: "M14 11v6",
			key: "outv1u"
		}],
		["path", {
			d: "M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6",
			key: "miytrc"
		}],
		["path", {
			d: "M3 6h18",
			key: "d0wm0j"
		}],
		["path", {
			d: "M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2",
			key: "e791ji"
		}]
	],
	aliases: ["trash-2"]
};
__iconData$1.node;
var Trash = createLucideIcon(__iconData$1);
//#endregion
//#region ../botgear/repo/webxr/1.0.0/ui/node_modules/lucide-react/dist/esm/icons/video.mjs
/**
* @license lucide-react v1.45.0 - ISC
*
* This source code is licensed under the ISC license.
* See the LICENSE file in the root directory of this source tree.
*/
var __iconData = {
	name: "video",
	size: 24,
	node: [["path", {
		d: "m16 13 5.223 3.482a.5.5 0 0 0 .777-.416V7.87a.5.5 0 0 0-.752-.432L16 10.5",
		key: "ftymec"
	}], ["rect", {
		x: "2",
		y: "6",
		width: "14",
		height: "12",
		rx: "2",
		key: "158x01"
	}]]
};
__iconData.node;
var Video = createLucideIcon(__iconData);
//#endregion
//#region ../botgear/repo/webxr/1.0.0/ui/View.tsx
var ANCHOR_DEFAULT = {
	head: {
		pos: [
			0,
			0,
			-1.5
		],
		quat: [
			0,
			0,
			0,
			1
		]
	},
	body: {
		pos: [
			0,
			-.1,
			-1.4
		],
		quat: [
			0,
			0,
			0,
			1
		]
	},
	wrist: {
		pos: [
			0,
			.04,
			-.08
		],
		quat: [
			0,
			0,
			0,
			1
		]
	},
	world: {
		pos: [
			0,
			1.4,
			-1.5
		],
		quat: [
			0,
			0,
			0,
			1
		]
	}
};
var fmt = (v, d = 0) => v ? `[${v.map((n) => Number(n ?? 0).toFixed(d)).join(", ")}]` : "—";
var shortId = (id) => id.replace(/^xr-standard-/, "");
function CompList({ c }) {
	const comps = c?.components ?? {};
	const ids = Object.keys(comps).sort();
	if (!ids.length) return /* @__PURE__ */ jsx("div", {
		className: "text-[10px] text-slate-600",
		children: "—"
	});
	return /* @__PURE__ */ jsx("div", {
		className: "space-y-0.5 font-mono text-[10px] leading-tight",
		children: ids.map((id) => {
			const k = comps[id] ?? {};
			const kx = k.x ?? 0, ky = k.y ?? 0, kv = k.value ?? 0, ks = k.state ?? "";
			const axes = Math.abs(kx) > .001 || Math.abs(ky) > .001 || id.includes("thumbstick");
			return /* @__PURE__ */ jsxs("div", {
				className: ks !== "default" || Math.abs(kv) > .02 || Math.abs(kx) > .02 || Math.abs(ky) > .02 ? "text-emerald-300" : "text-slate-600",
				title: `${id} · ${ks}`,
				children: [
					shortId(id),
					" ",
					ks.charAt(0),
					" ",
					kv.toFixed(2),
					axes ? ` (${kx.toFixed(2)},${ky.toFixed(2)})` : ""
				]
			}, id);
		})
	});
}
function NumIn({ v, on, step = "0.1", w = "w-12" }) {
	return /* @__PURE__ */ jsx("input", {
		type: "number",
		step,
		value: v,
		onChange: (e) => on(Number.isFinite(parseFloat(e.target.value)) ? parseFloat(e.target.value) : 0),
		className: `${w} rounded border border-slate-700 bg-slate-950 px-1 py-0.5 text-[10px]`
	});
}
function Vec3In({ v, on }) {
	const a = [
		v?.[0] ?? 0,
		v?.[1] ?? 0,
		v?.[2] ?? 0
	];
	return /* @__PURE__ */ jsx("span", {
		className: "inline-flex gap-0.5",
		children: a.map((n, i) => /* @__PURE__ */ jsx(NumIn, {
			v: n,
			w: "w-11",
			step: "1",
			on: (x) => {
				const b = [...a];
				b[i] = x;
				on(b);
			}
		}, i))
	});
}
function WebXRControlView({ proxy }) {
	const ws = useWsClient();
	const proxyId = proxy.id ?? proxy.name ?? "";
	const [type] = (proxy.service_meta_id ?? "webxr@1.0.0").split("@");
	const stateTopic = `/${type}/${proxyId}/state`;
	const controlTopic = `/${type}/${proxyId}/control`;
	const [state, setState] = useState({});
	const [err, setErr] = useState(null);
	const [expanded, setExpanded] = useState(null);
	const [robotArmed, setRobotArmed] = useState(false);
	useEffect(() => {
		const off = ws.subscribe(stateTopic, (f) => {
			if (f.method === "message") setState(f.payload);
		});
		return () => off();
	}, [ws, stateTopic]);
	const req = useServiceRequest(controlTopic, {
		timeoutMs: 15e3,
		errorField: "error",
		replyPrefix: `webxr-${proxyId}`
	});
	const call = useCallback(async (action, payload) => {
		try {
			await req.request(action, payload);
			setErr(null);
		} catch (e) {
			setErr(String(e?.message ?? e));
		}
	}, [req]);
	const runtimeId = useMemo(() => {
		const m = window.location.pathname.match(/^\/r\/([^/]+)/);
		return m ? m[1] : "runtime";
	}, []);
	const xrUrl = useMemo(() => `${window.location.origin}/r/${runtimeId}/xr/${encodeURIComponent(proxyId)}`, [runtimeId, proxyId]);
	const [vTitle, setVTitle] = useState("camera");
	const [vUrl, setVUrl] = useState("");
	const [tTitle, setTTitle] = useState("telemetry");
	const [tTopic, setTTopic] = useState("");
	const [bProxy, setBProxy] = useState("");
	const [bView, setBView] = useState("");
	const [mapTarget, setMapTarget] = useState("robot_kinematics-1");
	const addVideo = () => {
		if (!vUrl.trim()) return;
		call("set_panel", { panel: {
			id: `video-${Date.now().toString(36)}`,
			title: vTitle || "camera",
			source: {
				kind: "video_mjpeg",
				ref: vUrl.trim()
			},
			transform: {
				pos: [
					0,
					1.4,
					-1.5
				],
				quat: [
					0,
					0,
					0,
					1
				],
				width_m: 1.2,
				height_m: .7,
				scale: 1
			}
		} });
		setVUrl("");
	};
	const addTelemetry = () => {
		if (!tTopic.trim()) return;
		call("set_panel", { panel: {
			id: `telem-${Date.now().toString(36)}`,
			title: tTitle || "telemetry",
			source: {
				kind: "telemetry",
				ref: tTopic.trim()
			},
			transform: {
				pos: [
					1.4,
					1.4,
					-1
				],
				quat: [
					0,
					-.38,
					0,
					.92
				],
				width_m: .9,
				height_m: .6,
				scale: 1
			}
		} });
		setTTopic("");
	};
	const addBrowser = () => {
		const pid = bProxy.trim();
		if (!pid) return;
		const v = bView.trim();
		const ref = `/r/${encodeURIComponent(runtimeId)}/dock/${encodeURIComponent(pid)}` + (v ? `?view=${encodeURIComponent(v)}` : "");
		call("set_panel", { panel: {
			id: `web-${Date.now().toString(36)}`,
			title: pid,
			source: {
				kind: "browser",
				ref
			},
			transform: {
				pos: [
					-1.4,
					1.4,
					-1
				],
				quat: [
					0,
					.38,
					0,
					.92
				],
				width_m: 1,
				height_m: .7,
				scale: 1
			}
		} });
		setBProxy("");
		setBView("");
	};
	const calibrateTeleop = () => void call("calibrate_teleop", {});
	const updateMapping = (mp, patch) => void call("set_mapping", { mapping: {
		...mp,
		...patch
	} });
	const targetControl = (t) => {
		const s = t.trim();
		return `/${s.includes("/") ? s.split("/")[0] : s.match(/^(.*)-\d+$/)?.[1] ?? s}/${s}/control`;
	};
	const targetState = (t) => targetControl(t).replace(/\/control$/, "/state");
	const armHardware = (on) => ws.publish(targetControl(mapTarget), {
		action: "set_actuate_hardware",
		enabled: on
	});
	useEffect(() => {
		if (!mapTarget.trim()) return;
		const off = ws.subscribe(targetState(mapTarget), (f) => {
			if (f.method === "message") setRobotArmed(!!f.payload?.actuate_hardware);
		});
		return () => off();
	}, [ws, mapTarget]);
	const sess = state.session ?? {};
	const dot = (ok) => /* @__PURE__ */ jsx("span", { style: {
		display: "inline-block",
		width: 8,
		height: 8,
		borderRadius: 8,
		background: ok ? "#34d399" : "#64748b"
	} });
	return /* @__PURE__ */ jsxs("div", {
		className: "space-y-3 p-3 text-xs text-slate-200",
		children: [
			/* @__PURE__ */ jsxs("div", {
				className: "flex items-center gap-2",
				children: [
					dot(state.connected),
					/* @__PURE__ */ jsx("span", {
						className: "font-medium",
						children: state.connected ? `headset connected · ${sess.mode ?? "vr"} · ${Math.round(sess.fps ?? 0)} fps` : "no headset — open the view on the Quest"
					}),
					/* @__PURE__ */ jsx("button", {
						onClick: () => call("set_enabled", { enabled: !(state.enabled ?? true) }),
						className: `ml-auto rounded px-2 py-0.5 ${state.enabled ?? true ? "bg-emerald-800/60 text-emerald-200" : "border border-slate-700 text-slate-400"}`,
						children: state.enabled ?? true ? "enabled" : "disabled"
					})
				]
			}),
			/* @__PURE__ */ jsxs("div", {
				className: "rounded border border-slate-800 bg-slate-950/60 p-2",
				children: [
					/* @__PURE__ */ jsxs("div", {
						className: "mb-1 flex items-center gap-1 text-[10px] uppercase tracking-wider text-slate-400",
						children: [/* @__PURE__ */ jsx(Eye, { className: "h-3 w-3" }), " open on headset"]
					}),
					/* @__PURE__ */ jsx("a", {
						href: xrUrl,
						target: "_blank",
						rel: "noreferrer",
						className: "block break-all rounded bg-slate-900 px-2 py-1 text-[11px] text-sky-300 underline decoration-sky-700 hover:bg-slate-800 hover:text-sky-200",
						children: xrUrl
					}),
					/* @__PURE__ */ jsxs("p", {
						className: "mt-1 text-[10px] text-slate-500",
						children: [
							"Open this on the Quest 3 Browser and press Enter VR. Dev over USB:",
							" ",
							/* @__PURE__ */ jsx("code", { children: "adb reverse tcp:5051 tcp:5051" }),
							" first, so the headset reaches it at localhost."
						]
					})
				]
			}),
			/* @__PURE__ */ jsxs("div", {
				className: "rounded border border-slate-800 bg-slate-950/60 p-2",
				children: [
					/* @__PURE__ */ jsxs("div", {
						className: "mb-1 flex items-center gap-1 text-[10px] uppercase tracking-wider text-slate-400",
						children: [/* @__PURE__ */ jsx(Crosshair, { className: "h-3 w-3" }), " telemetry (robot frame, mm)"]
					}),
					/* @__PURE__ */ jsxs("div", {
						className: "grid grid-cols-[64px_1fr] gap-x-2 gap-y-0.5 font-mono text-[11px]",
						children: [
							/* @__PURE__ */ jsx("span", {
								className: "text-slate-500",
								children: "head"
							}),
							/* @__PURE__ */ jsx("span", { children: fmt(state.head?.pos) }),
							/* @__PURE__ */ jsx("span", {
								className: "text-slate-500",
								children: "L ray"
							}),
							/* @__PURE__ */ jsx("span", { children: fmt(state.controller?.left?.ray?.pos) }),
							/* @__PURE__ */ jsx("span", {
								className: "text-slate-500",
								children: "R ray"
							}),
							/* @__PURE__ */ jsx("span", { children: fmt(state.controller?.right?.ray?.pos) }),
							/* @__PURE__ */ jsx("span", {
								className: "text-slate-500",
								children: "R axes"
							}),
							/* @__PURE__ */ jsx("span", { children: state.controller?.right?.axes ? `x ${(state.controller.right.axes.x ?? 0).toFixed(2)} y ${(state.controller.right.axes.y ?? 0).toFixed(2)}` : "—" }),
							/* @__PURE__ */ jsx("span", {
								className: "text-slate-500",
								children: "origin"
							}),
							/* @__PURE__ */ jsx("span", { children: fmt(state.origin_mm) })
						]
					}),
					/* @__PURE__ */ jsxs("div", {
						className: "mt-1 flex gap-1",
						children: [/* @__PURE__ */ jsx("button", {
							onClick: () => call("recenter"),
							disabled: !state.head,
							className: "rounded bg-sky-900/60 px-2 py-0.5 text-sky-200 hover:bg-sky-800/60 disabled:opacity-40",
							children: "Recenter"
						}), /* @__PURE__ */ jsx("button", {
							onClick: () => call("clear_origin"),
							className: "rounded border border-slate-700 px-2 py-0.5 text-slate-300 hover:border-slate-500",
							children: "Clear origin"
						})]
					})
				]
			}),
			/* @__PURE__ */ jsxs("div", {
				className: "rounded border border-slate-800 bg-slate-950/60 p-2",
				children: [/* @__PURE__ */ jsxs("div", {
					className: "mb-1 flex items-center gap-1 text-[10px] uppercase tracking-wider text-slate-400",
					children: [
						/* @__PURE__ */ jsx(Gamepad2, { className: "h-3 w-3" }),
						" controller inputs",
						/* @__PURE__ */ jsx("span", {
							className: "ml-auto normal-case text-slate-600",
							children: "on /controller/{left,right}"
						})
					]
				}), /* @__PURE__ */ jsx("div", {
					className: "grid grid-cols-2 gap-2",
					children: ["left", "right"].map((side) => /* @__PURE__ */ jsxs("div", { children: [/* @__PURE__ */ jsx("div", {
						className: "mb-0.5 text-[9px] uppercase tracking-wider text-slate-500",
						children: side
					}), /* @__PURE__ */ jsx(CompList, { c: state.controller?.[side] ?? null })] }, side))
				})]
			}),
			/* @__PURE__ */ jsxs("div", {
				className: "rounded border border-slate-800 bg-slate-950/60 p-2",
				children: [
					/* @__PURE__ */ jsxs("div", {
						className: "mb-1 flex items-center gap-1 text-[10px] uppercase tracking-wider text-slate-400",
						children: [
							/* @__PURE__ */ jsx(Video, { className: "h-3 w-3" }),
							" feeds (",
							(state.panels ?? []).length,
							")"
						]
					}),
					/* @__PURE__ */ jsx("div", {
						className: "space-y-0.5",
						children: (state.panels ?? []).map((p) => /* @__PURE__ */ jsxs("div", {
							className: "flex items-center gap-1",
							children: [
								/* @__PURE__ */ jsx("span", {
									className: "rounded bg-slate-800 px-1 text-[9px] uppercase text-slate-400",
									children: p.source.kind === "telemetry" ? "tlm" : p.source.kind === "browser" ? "web" : "vid"
								}),
								/* @__PURE__ */ jsx("span", {
									className: "min-w-0 flex-1 truncate font-mono text-[11px]",
									title: p.source.ref,
									children: p.title || p.source.ref
								}),
								/* @__PURE__ */ jsxs("select", {
									value: p.placement ?? "world",
									onChange: (e) => {
										const a = e.target.value;
										const off = ANCHOR_DEFAULT[a];
										call("set_panel", { panel: {
											...p,
											placement: a,
											transform: {
												...p.transform,
												pos: off.pos,
												quat: off.quat
											}
										} });
									},
									title: "Anchor frame (head = locks in front of gaze)",
									className: "shrink-0 rounded border border-slate-700 bg-slate-950 px-1 py-0.5 text-[10px]",
									children: [
										/* @__PURE__ */ jsx("option", {
											value: "world",
											children: "world"
										}),
										/* @__PURE__ */ jsx("option", {
											value: "head",
											children: "head"
										}),
										/* @__PURE__ */ jsx("option", {
											value: "body",
											children: "body"
										}),
										/* @__PURE__ */ jsx("option", {
											value: "wrist",
											children: "wrist"
										})
									]
								}),
								/* @__PURE__ */ jsx("button", {
									onClick: () => call("remove_panel", { id: p.id }),
									className: "shrink-0 rounded p-0.5 text-slate-500 hover:bg-rose-900/40 hover:text-rose-300",
									children: /* @__PURE__ */ jsx(Trash, { className: "h-3 w-3" })
								})
							]
						}, p.id))
					}),
					/* @__PURE__ */ jsxs("div", {
						className: "mt-1.5 space-y-1 border-t border-slate-800 pt-1.5",
						children: [
							/* @__PURE__ */ jsxs("div", {
								className: "flex items-center gap-1",
								children: [
									/* @__PURE__ */ jsx("input", {
										value: vTitle,
										onChange: (e) => setVTitle(e.target.value),
										placeholder: "title",
										className: "w-20 rounded border border-slate-700 bg-slate-950 px-1 py-0.5 text-[11px]"
									}),
									/* @__PURE__ */ jsx("input", {
										value: vUrl,
										onChange: (e) => setVUrl(e.target.value),
										placeholder: "stream id e.g. video/video-1 (or full MJPEG url)",
										className: "min-w-0 flex-1 rounded border border-slate-700 bg-slate-950 px-1 py-0.5 text-[11px]"
									}),
									/* @__PURE__ */ jsx("button", {
										onClick: addVideo,
										title: "Add video feed",
										className: "rounded bg-slate-800 px-1.5 py-0.5 hover:bg-slate-700",
										children: /* @__PURE__ */ jsx(Plus, { className: "h-3 w-3" })
									})
								]
							}),
							/* @__PURE__ */ jsxs("div", {
								className: "flex items-center gap-1",
								children: [
									/* @__PURE__ */ jsx("input", {
										value: tTitle,
										onChange: (e) => setTTitle(e.target.value),
										placeholder: "title",
										className: "w-20 rounded border border-slate-700 bg-slate-950 px-1 py-0.5 text-[11px]"
									}),
									/* @__PURE__ */ jsx("input", {
										value: tTopic,
										onChange: (e) => setTTopic(e.target.value),
										placeholder: "bus topic (telemetry)",
										className: "min-w-0 flex-1 rounded border border-slate-700 bg-slate-950 px-1 py-0.5 text-[11px]"
									}),
									/* @__PURE__ */ jsx("button", {
										onClick: addTelemetry,
										title: "Add telemetry panel",
										className: "rounded bg-slate-800 px-1.5 py-0.5 hover:bg-slate-700",
										children: /* @__PURE__ */ jsx(Plus, { className: "h-3 w-3" })
									})
								]
							}),
							/* @__PURE__ */ jsxs("div", {
								className: "flex items-center gap-1",
								children: [
									/* @__PURE__ */ jsx("span", {
										title: "Service UI in VR",
										className: "flex shrink-0",
										children: /* @__PURE__ */ jsx(Globe, { className: "h-3 w-3 text-slate-500" })
									}),
									/* @__PURE__ */ jsx("input", {
										value: bProxy,
										onChange: (e) => setBProxy(e.target.value),
										placeholder: "service proxy id e.g. servo-1",
										className: "min-w-0 flex-1 rounded border border-slate-700 bg-slate-950 px-1 py-0.5 text-[11px]"
									}),
									/* @__PURE__ */ jsx("input", {
										value: bView,
										onChange: (e) => setBView(e.target.value),
										placeholder: "view (optional)",
										className: "w-20 rounded border border-slate-700 bg-slate-950 px-1 py-0.5 text-[11px]"
									}),
									/* @__PURE__ */ jsx("button", {
										onClick: addBrowser,
										title: "Add service UI panel (browser)",
										className: "rounded bg-slate-800 px-1.5 py-0.5 hover:bg-slate-700",
										children: /* @__PURE__ */ jsx(Plus, { className: "h-3 w-3" })
									})
								]
							})
						]
					})
				]
			}),
			/* @__PURE__ */ jsxs("div", {
				className: "rounded border border-slate-800 bg-slate-950/60 p-2",
				children: [
					/* @__PURE__ */ jsxs("div", {
						className: "mb-1 flex items-center gap-1 text-[10px] uppercase tracking-wider text-slate-400",
						children: [
							/* @__PURE__ */ jsx(Gamepad2, { className: "h-3 w-3" }),
							" mappings (",
							(state.mappings ?? []).length,
							")"
						]
					}),
					/* @__PURE__ */ jsx("div", {
						className: "space-y-0.5",
						children: (state.mappings ?? []).map((m) => {
							const open = expanded === m.id;
							const isPose = m.action === "set_target";
							return /* @__PURE__ */ jsxs("div", {
								className: "rounded border border-slate-900 bg-slate-950/40",
								children: [/* @__PURE__ */ jsxs("div", {
									className: "flex items-center gap-1 px-1 py-0.5 font-mono text-[11px]",
									children: [
										/* @__PURE__ */ jsx("button", {
											onClick: () => setExpanded(open ? null : m.id),
											className: "shrink-0 text-slate-500 hover:text-slate-300",
											title: "edit",
											children: open ? "▾" : "▸"
										}),
										/* @__PURE__ */ jsxs("span", {
											className: "truncate",
											title: `${m.source} → ${m.target}.${m.action}`,
											children: [
												m.source,
												" → ",
												m.action
											]
										}),
										m.enable_source ? /* @__PURE__ */ jsx("span", {
											className: "shrink-0 text-amber-500",
											title: `deadman: ${m.enable_source}`,
											children: "◉"
										}) : null,
										/* @__PURE__ */ jsx("button", {
											onClick: () => call("remove_mapping", { id: m.id }),
											className: "ml-auto rounded p-0.5 text-slate-500 hover:bg-rose-900/40 hover:text-rose-300",
											children: /* @__PURE__ */ jsx(Trash, { className: "h-3 w-3" })
										})
									]
								}), open && /* @__PURE__ */ jsxs("div", {
									className: "space-y-1 border-t border-slate-900 px-2 py-1 text-[10px] text-slate-400",
									children: [isPose ? /* @__PURE__ */ jsxs(Fragment, { children: [
										/* @__PURE__ */ jsxs("label", {
											className: "flex items-center gap-1",
											children: ["scale3 ", /* @__PURE__ */ jsx(Vec3In, {
												v: m.scale3,
												on: (a) => updateMapping(m, { scale3: a })
											})]
										}),
										/* @__PURE__ */ jsxs("label", {
											className: "flex items-center gap-1",
											title: "shoulder/world anchor (mm) — the teleop reach centre",
											children: ["offset3 ", /* @__PURE__ */ jsx(Vec3In, {
												v: m.offset3,
												on: (a) => updateMapping(m, { offset3: a })
											})]
										}),
										/* @__PURE__ */ jsxs("label", {
											className: "flex items-center gap-1",
											children: ["clamp− ", /* @__PURE__ */ jsx(Vec3In, {
												v: m.clamp_min,
												on: (a) => updateMapping(m, { clamp_min: a })
											})]
										}),
										/* @__PURE__ */ jsxs("label", {
											className: "flex items-center gap-1",
											children: ["clamp+ ", /* @__PURE__ */ jsx(Vec3In, {
												v: m.clamp_max,
												on: (a) => updateMapping(m, { clamp_max: a })
											})]
										})
									] }) : /* @__PURE__ */ jsxs("div", {
										className: "flex flex-wrap items-center gap-2",
										children: [
											/* @__PURE__ */ jsxs("label", {
												className: "flex items-center gap-1",
												children: ["scale ", /* @__PURE__ */ jsx(NumIn, {
													v: m.scale ?? 1,
													on: (x) => updateMapping(m, { scale: x })
												})]
											}),
											/* @__PURE__ */ jsxs("label", {
												className: "flex items-center gap-1",
												children: ["offset ", /* @__PURE__ */ jsx(NumIn, {
													v: m.offset ?? 0,
													on: (x) => updateMapping(m, { offset: x })
												})]
											}),
											/* @__PURE__ */ jsxs("label", {
												className: "flex items-center gap-1",
												title: "ignore |value| below this",
												children: ["dead ", /* @__PURE__ */ jsx(NumIn, {
													v: m.deadzone ?? 0,
													step: "0.01",
													on: (x) => updateMapping(m, { deadzone: x })
												})]
											})
										]
									}), /* @__PURE__ */ jsxs("div", {
										className: "flex flex-wrap items-center gap-2",
										children: [/* @__PURE__ */ jsxs("label", {
											className: "flex items-center gap-1",
											title: "smoothing 0..1 (higher = smoother)",
											children: ["ema ", /* @__PURE__ */ jsx(NumIn, {
												v: m.ema ?? 0,
												step: "0.05",
												on: (x) => updateMapping(m, { ema: x })
											})]
										}), /* @__PURE__ */ jsxs("label", {
											className: "flex min-w-0 flex-1 items-center gap-1",
											title: "deadman: only fires while this signal ≥ min",
											children: ["grip", /* @__PURE__ */ jsx("input", {
												value: m.enable_source ?? "",
												onChange: (e) => updateMapping(m, { enable_source: e.target.value }),
												placeholder: "controller.right.buttons.grip",
												className: "min-w-0 flex-1 rounded border border-slate-700 bg-slate-950 px-1 py-0.5 text-[10px]"
											})]
										})]
									})]
								})]
							}, m.id);
						})
					}),
					/* @__PURE__ */ jsxs("div", {
						className: "mt-1.5 flex items-center gap-1 border-t border-slate-800 pt-1.5",
						children: [
							/* @__PURE__ */ jsx("input", {
								value: mapTarget,
								onChange: (e) => setMapTarget(e.target.value),
								placeholder: "robot_kinematics proxy id",
								className: "min-w-0 flex-1 rounded border border-slate-700 bg-slate-950 px-1 py-0.5 text-[11px]"
							}),
							/* @__PURE__ */ jsx("button", {
								onClick: () => call("set_teleop", {
									enabled: !state.teleop_enabled,
									target: mapTarget
								}),
								title: "Toggle InMoov teleop — installs/removes the mapping set + preps the robot. Stays until toggled off.",
								className: `rounded px-2 py-0.5 text-[11px] ${state.teleop_enabled ? "bg-emerald-700/70 text-emerald-100 hover:bg-emerald-600/70" : "bg-sky-900/60 text-sky-200 hover:bg-sky-800/60"}`,
								children: state.teleop_enabled ? "● Teleop ON" : "Teleop off"
							}),
							/* @__PURE__ */ jsx("button", {
								onClick: calibrateTeleop,
								title: "Recenter first, then hold both arms STRAIGHT FORWARD and click — auto-fits reach scale + shoulder anchor so your hands pair to the robot's",
								className: "rounded bg-emerald-900/60 px-2 py-0.5 text-[11px] text-emerald-200 hover:bg-emerald-800/60",
								children: "Calibrate"
							})
						]
					}),
					/* @__PURE__ */ jsxs("div", {
						className: "mt-1.5 flex items-center gap-2 border-t border-slate-800 pt-1.5 text-[11px]",
						children: [/* @__PURE__ */ jsx("button", {
							onClick: () => armHardware(!robotArmed),
							title: "Arm physical servo output on the target — the REAL robot follows teleop",
							className: `rounded px-2 py-0.5 ${robotArmed ? "bg-rose-800/70 text-rose-100 hover:bg-rose-700/70" : "bg-slate-800 text-slate-300 hover:bg-slate-700"}`,
							children: robotArmed ? "● HARDWARE ARMED" : "○ arm hardware"
						}), /* @__PURE__ */ jsxs("span", {
							className: "text-[10px] text-slate-500",
							children: [
								robotArmed ? "real servos move" : "model only",
								" on ",
								mapTarget || "—"
							]
						})]
					})
				]
			}),
			err && /* @__PURE__ */ jsx("div", {
				className: "truncate font-mono text-[10px] text-rose-300",
				title: err,
				children: err
			})
		]
	});
}
//#endregion
export { WebXRControlView as default };
