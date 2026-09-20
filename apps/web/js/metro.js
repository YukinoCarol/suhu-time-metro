// 时间地铁图：把「起点→各枢纽」的分钟数画成放射状地铁图（站距=时间）
import { MODE_HEX, bearingDeg } from "./core.js";

const W = 800, H = 660, CX = 400, CY = 358, RMAX = 278;
// 半径刻度随当前模式的最大分钟数自适应（drawRings/renderMetro 内更新）
let RING_MAX = 95;

const rOf = (t) => (Math.min(t ?? RING_MAX, RING_MAX) / RING_MAX) * RMAX;
const pos = (t, deg) => {
  const r = rOf(t), a = deg * Math.PI / 180;
  return [CX + r * Math.sin(a), CY - r * Math.cos(a)];
};
const shortName = (s) => s.replace(/（.*?）|\(.*?\)/g, "");

// 选取"恰好装下 5 圈"的漂亮刻度：5/10/15/20/25/30/45/60…
function niceStep(raw) {
  const cands = [5, 10, 15, 20, 25, 30, 40, 45, 60, 75, 90, 120, 150];
  return cands.find(c => c >= raw) || Math.ceil(raw / 30) * 30;
}

function ensureSkeleton(svg) {
  if (svg.select(".g-rings").node()) return;
  svg.append("g").attr("class", "g-rings");
  svg.append("g").attr("class", "g-spokes");
  svg.append("g").attr("class", "g-stations");
  svg.append("g").attr("class", "g-labels");
}

function drawRings(svg, rings) {
  const g = svg.select(".g-rings");
  const sel = g.selectAll("circle").data(rings, d => d);
  sel.enter().append("circle")
    .attr("cx", CX).attr("cy", CY)
    .attr("r", d => rOf(d))
    .attr("fill", "none")
    .attr("stroke", "#d6dbe1").attr("stroke-width", 1.2).attr("stroke-dasharray", "3 5")
    .merge(sel)
    .attr("r", d => rOf(d));
  sel.exit().remove();

  const lab = g.selectAll("text").data(rings, d => d);
  lab.enter().append("text")
    .attr("x", d => CX + rOf(d) * 0.7071 + 5)
    .attr("y", d => CY - rOf(d) * 0.7071 - 5)
    .attr("class", "ringLabel")
    .attr("font-size", 10.5)
    .attr("fill", "#9aa4ae")
    .text(d => `${d}′`)
    .merge(lab)
    .attr("x", d => CX + rOf(d) * 0.7071 + 5)
    .attr("y", d => CY - rOf(d) * 0.7071 - 5);
  lab.exit().remove();
}

export function renderMetro(svg, opts) {
  const { hubs, times, mode, destId, originName, onSelect } = opts;
  ensureSkeleton(svg);

  const all = hubs
    .filter(h => h.id !== (opts.originId || ""))
    .map(h => ({ ...h, t: times?.[mode]?.[h.id] }));

  // 半径刻度自适应：5 圈刻度恰好罩住本模式的最大分钟数
  const tvals = all.map(n => n.t).filter(t => t != null);
  const maxT = tvals.length ? Math.max(...tvals) : 95;
  const step = niceStep(maxT / 5 * 1.04);
  RING_MAX = step * 5;
  const rings = [1, 2, 3, 4, 5].map(k => k * step);
  drawRings(svg, rings);

  const nodes = all.map(h => {
    const deg = bearingDeg(opts.originCoord, [h.lng, h.lat]);
    const [x, y] = pos(h.t, deg);
    return { ...h, deg, x, y };
  });

  // 标签贪心防压盖：按方位角排序；碰撞时沿切向（垂直辐条）散开，
  // 优先尝试位移最小且无碰撞的位置
  nodes.sort((a, b) => a.deg - b.deg);
  const labelBox = (n, t) => {
    const a = n.deg * Math.PI / 180;
    const rx = n.x + 17 * Math.sin(a) * 0.9 + t * Math.cos(a);
    const ry = n.y - 17 * Math.cos(a) + t * Math.sin(a);
    const name = shortName(n.name);
    const w = name.length * 11.5 + 6;
    const dx = Math.sin(a) * 17 * 0.9;
    const anchor = dx > 6 ? "start" : dx < -6 ? "end" : "middle";
    const x0 = anchor === "start" ? rx : anchor === "end" ? rx - w : rx - w / 2;
    return { x0: x0 - 3, y0: ry - 13, x1: x0 + w + 3, y1: ry + 4, anchor, lx: rx, ly: ry };
  };
  const placed = [];
  nodes.forEach(n => {
    let best = null;
    for (let k = 0; k <= 10 && !best; k++) {
      for (const s of (k === 0 ? [0] : [k, -k])) {
        const box = labelBox(n, s * 18);
        const hit = placed.some(p =>
          p.x0 < box.x1 && box.x0 < p.x1 && p.y0 < box.y1 && box.y0 < p.y1);
        if (!hit) { best = box; break; }
      }
    }
    if (!best) best = labelBox(n, 144);
    n.labelXY = [best.lx, best.ly];
    n.anchor = best.anchor;
    placed.push(best);
  });

  const T = d3.transition().duration(650).ease(d3.easeCubicOut);

  // 辐条
  const spokes = svg.select(".g-spokes").selectAll("line").data(nodes, d => d.id);
  spokes.enter().append("line")
    .attr("x1", CX).attr("y1", CY)
    .attr("x2", CX).attr("y2", CY)
    .attr("stroke", MODE_HEX[mode]).attr("stroke-opacity", 0.5)
    .attr("stroke-width", 5).attr("stroke-linecap", "round")
    .merge(spokes)
    .transition(T)
    .attr("x2", d => d.x).attr("y2", d => d.y)
    .attr("stroke", MODE_HEX[mode]);
  spokes.exit().remove();

  // 高铁站外环
  const outer = svg.select(".g-stations").selectAll(".outer").data(
    nodes.filter(n => n.hsr), d => d.id);
  outer.enter().append("circle").attr("class", "outer")
    .attr("cx", CX).attr("cy", CY).attr("r", 12.5)
    .attr("fill", "none").attr("stroke", MODE_HEX[mode]).attr("stroke-width", 2)
    .attr("stroke-dasharray", "2.5 2.5")
    .on("click", (e, d) => onSelect(d.id))
    .merge(outer)
    .transition(T)
    .attr("cx", d => d.x).attr("cy", d => d.y)
    .attr("stroke", MODE_HEX[mode]);
  outer.exit().remove();

  // 站点圆
  const st = svg.select(".g-stations").selectAll(".st").data(nodes, d => d.id);
  st.enter().append("circle").attr("class", "st")
    .attr("cx", CX).attr("cy", CY).attr("r", 7.5)
    .attr("fill", "#fff").attr("stroke", "#141b22").attr("stroke-width", 3)
    .style("cursor", "pointer")
    .on("click", (e, d) => onSelect(d.id))
    .merge(st)
    .attr("title", d => `${d.name} · ${d.t == null ? "—" : d.t + "′"}`)
    .transition(T)
    .attr("cx", d => d.x).attr("cy", d => d.y)
    .attr("r", d => d.id === destId ? 10 : 7.5)
    .attr("fill", d => d.id === destId ? MODE_HEX[mode] : "#fff");

  // 内芯（选中站白点）
  const cores = svg.select(".g-stations").selectAll(".core").data(
    nodes.filter(d => d.id === destId), d => d.id);
  cores.enter().append("circle").attr("class", "core")
    .attr("r", 3.5).attr("fill", "#fff")
    .on("click", (e, d) => onSelect(d.id))
    .merge(cores)
    .transition(T)
    .attr("cx", d => d.x).attr("cy", d => d.y);
  cores.exit().remove();

  // 标注（短名；完整名字放 title 提示）
  const labels = svg.select(".g-labels").selectAll("text").data(nodes, d => d.id);
  labels.enter().append("text")
    .attr("font-size", 11.5)
    .attr("fill", "#39434d")
    .style("cursor", "pointer")
    .on("click", (e, d) => onSelect(d.id))
    .merge(labels)
    .attr("title", d => `${d.name} · ${d.t == null ? "—" : d.t + "′"}`)
    .text(d => shortName(d.name))
    .transition(T)
    .attr("x", d => d.labelXY[0])
    .attr("y", d => d.labelXY[1] + 4)
    .attr("text-anchor", d => d.anchor);
  labels.exit().remove();

  // 起点菱形
  let o = svg.select(".g-origin");
  if (!o.node()) {
    o = svg.append("g").attr("class", "g-origin");
    o.append("rect").attr("width", 15).attr("height", 15)
      .attr("fill", "#141b22").attr("transform", `rotate(45 ${CX} ${CY})`)
      .attr("x", CX - 7.5).attr("y", CY - 7.5);
    o.append("text").attr("x", CX).attr("y", CY + 26)
      .attr("text-anchor", "middle").attr("font-size", 11.5)
      .attr("fill", "#141b22").attr("font-weight", 700);
  }
  o.select("text").text(`从 · ${originName}`);
}
