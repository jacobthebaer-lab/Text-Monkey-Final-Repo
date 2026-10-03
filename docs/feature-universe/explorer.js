"use strict";
(() => {
  const model = JSON.parse(document.getElementById("model-data").textContent);
  const $ = (id) => document.getElementById(id),
    canvas = $("space"),
    ctx = canvas.getContext("2d");
  const colors = [
    "#ffd23f",
    "#80d7f0",
    "#b6aaff",
    "#ffae8e",
    "#9ddebb",
    "#f0a8d8",
    "#91b8fa",
    "#e7d0a0",
    "#9bd6d4",
    "#b9cf84",
    "#d6b8eb",
    "#ed9c98",
    "#c3cde0",
    "#95c3a6",
  ];
  const statuses = {
    implemented: "In code",
    partial: "Partial / held",
    planned: "Requested",
    historical: "Earlier direction",
  };
  const statusNotes = {
    implemented:
      "Present in the inspected code or documented implementation. This label does not certify a running deployment or actual text delivery.",
    partial:
      "Some pieces exist, but integration, activation, current policy, or live verification is still incomplete.",
    planned:
      "Requested in the product vision or chat history; the full capability is not established in the inspected implementation.",
    historical:
      "An earlier proposal or behavior kept for context. Later requirements changed the direction.",
  };
  const features = model.categories.flatMap((c) =>
    c.features.map((f) => ({ ...f, category: c.id })),
  );
  const featureMap = new Map(features.map((f) => [f.id, f]));
  const categoryMap = new Map(
    model.categories.map((c, i) => [
      c.id,
      { ...c, color: colors[i % colors.length] },
    ]),
  );
  const esc = (s) =>
    String(s ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  const vec = (x = 0, y = 0, z = 0) => ({ x, y, z }),
    add = (a, b) => vec(a.x + b.x, a.y + b.y, a.z + b.z),
    sub = (a, b) => vec(a.x - b.x, a.y - b.y, a.z - b.z),
    mul = (a, s) => vec(a.x * s, a.y * s, a.z * s),
    dot = (a, b) => a.x * b.x + a.y * b.y + a.z * b.z,
    cross = (a, b) =>
      vec(a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x),
    len = (a) => Math.hypot(a.x, a.y, a.z),
    norm = (a) => mul(a, 1 / (len(a) || 1)),
    lerp = (a, b, t) => add(a, mul(sub(b, a), t));
  let seed = 7381;
  const rand = () => {
    seed = (seed * 1664525 + 1013904223) >>> 0;
    return seed / 4294967296;
  };
  let width = innerWidth,
    height = innerHeight,
    dpr = Math.min(devicePixelRatio || 1, 2),
    view = "galaxy",
    mode = "orbit",
    filterCategory = null,
    selected = null,
    hover = null,
    query = "",
    status = "all",
    labels = false,
    motion = !matchMedia("(prefers-reduced-motion: reduce)").matches,
    paused = false;
  let orbit = { yaw: 0.1, pitch: 0.18, distance: 1850, target: vec() },
    desired = { distance: 1850, target: vec() },
    flight = { position: vec(), yaw: 0, pitch: 0 },
    camera,
    forward,
    right,
    up;
  let detailOpener = null;
  let points = [],
    links = [],
    projected = [],
    pointer = null,
    dragged = false,
    last = 0,
    time = 0,
    tourIndex = -1,
    activeFlow = model.flows[0],
    path = [],
    decisionSelected = null;
  const keys = new Set(),
    stars = Array.from({ length: 1350 }, () => {
      const a = rand() * Math.PI * 2,
        u = rand() * 2 - 1,
        r = 1700 + rand() * 6000;
      return {
        p: vec(
          Math.cos(a) * Math.sqrt(1 - u * u) * r,
          u * r,
          Math.sin(a) * Math.sqrt(1 - u * u) * r,
        ),
        size: 0.4 + rand() * 1.5,
        alpha: 0.2 + rand() * 0.65,
      };
    });
  const root = {
    id: "universe",
    title: "Text Monkey",
    type: "root",
    p: vec(),
    color: colors[0],
    radius: 23,
  };
  const galaxy = [root],
    galaxyLinks = [];
  model.categories.forEach((raw, i) => {
    const c = categoryMap.get(raw.id),
      a = i * 2.3999632297,
      r = 440 + 80 * (i % 3),
      p = vec(
        Math.cos(a) * r,
        ((i % 3) - 1) * 260 + Math.sin(a * 1.3) * 115,
        Math.sin(a) * r * 0.77,
      );
    const center = { ...c, type: "category", p, radius: 12 };
    galaxy.push(center);
    galaxyLinks.push([root, center]);
    raw.features.forEach((f, j) => {
      const a = j * 2.3999632297,
        u = 1 - (2 * (j + 0.5)) / raw.features.length,
        r = 85 + Math.sqrt(j) * 23;
      const p2 = add(
        p,
        vec(
          Math.cos(a) * Math.sqrt(1 - u * u) * r,
          u * r * 0.8,
          Math.sin(a) * Math.sqrt(1 - u * u) * r,
        ),
      );
      const n = {
        ...f,
        category: c.id,
        type: "feature",
        p: p2,
        color: c.color,
        radius: 4.3,
      };
      galaxy.push(n);
      galaxyLinks.push([center, n]);
    });
  });
  const galaxyMap = new Map(galaxy.map((n) => [n.id, n]));
  function matches(f) {
    return (
      (status === "all" || f.status === status) &&
      (!filterCategory || f.category === filterCategory) &&
      (!query ||
        [
          f.title,
          f.summary,
          ...(f.details || []),
          categoryMap.get(f.category)?.title,
        ]
          .join(" ")
          .toLowerCase()
          .includes(query))
    );
  }
  function visibleFeatures() {
    return features.filter(matches);
  }
  function sourceHTML(s) {
    if (typeof s === "string") {
      const match = s.match(/^(.+?)(?::(\d+)(?:-\d+)?)?$/);
      const path = match ? match[1] : s;
      return `<a class="source" href="https://github.com/jacobthebaer-lab/text-monkey/blob/${encodeURIComponent(model.revision || "codex/complete-text-monkey")}/${path.split("/").map(encodeURIComponent).join("/")}${match?.[2] ? "#L" + match[2] : ""}" target="_blank" rel="noopener">${esc(s)}</a>`;
    }
    if (s.chatId)
      return `<a class="source" href="codex://threads/${encodeURIComponent(s.chatId)}">Chat: ${esc(s.title)}</a>`;
    return `<span class="source">${esc(s.title || s.path || JSON.stringify(s))}</span>`;
  }
  function renderDirectory() {
    const list = $("directory-list"),
      visible = visibleFeatures();
    $("feature-count").textContent =
      `${visible.length} / ${features.length} features`;
    if (query || status !== "all" || filterCategory) {
      list.innerHTML =
        (filterCategory
          ? `<button class="result-button" data-action="all">← All constellations</button>`
          : "") +
        visible
          .map(
            (f) =>
              `<button class="result-button" data-feature="${f.id}">${esc(f.title)}<small>${esc(categoryMap.get(f.category)?.title)} · ${statuses[f.status]}</small></button>`,
          )
          .join("");
      if (!visible.length)
        list.innerHTML +=
          '<p class="empty">No matching features. Try a shorter phrase or reset the filters.</p>';
    } else {
      list.innerHTML = model.categories
        .map(
          (c) =>
            `<button class="category-button" data-category="${c.id}"><span class="dot" style="--cat:${categoryMap.get(c.id).color}"></span><span>${esc(c.title)}</span><span class="count">${c.features.length}</span></button>`,
        )
        .join("");
    }
    if (view === "matrix") renderMatrix();
  }
  function renderMatrix() {
    const rows = visibleFeatures();
    $("matrix-count").textContent = `${rows.length} features`;
    $("matrix-rows").innerHTML =
      rows
        .map(
          (f) =>
            `<tr><td><button data-feature="${f.id}">${esc(f.title)}</button></td><td>${esc(categoryMap.get(f.category)?.title)}</td><td><span class="badge ${f.status}">${statuses[f.status]}</span></td><td>${esc(f.summary)}</td></tr>`,
        )
        .join("") || '<tr><td colspan="4">No matching features.</td></tr>';
  }
  function showDetail(html) {
    if ($("detail").hidden) detailOpener = document.activeElement;
    $("detail").innerHTML = html;
    $("detail").hidden = false;
    $("matrix").classList.add("with-detail");
    $("detail").scrollTop = 0;
    const heading = $("detail").querySelector("h2");
    if (heading) {
      heading.tabIndex = -1;
      heading.focus({ preventScroll: true });
    }
  }
  function closeDetail() {
    selected = null;
    decisionSelected = null;
    $("detail").hidden = true;
    $("matrix").classList.remove("with-detail");
    if (detailOpener?.isConnected) detailOpener.focus({ preventScroll: true });
  }
  function selectFeature(id, travel = true) {
    const f = featureMap.get(id);
    if (!f) return;
    if (!matches(f)) {
      filterCategory = null;
      query = "";
      status = "all";
      $("search").value = "";
      $("status-filter").value = "all";
      renderDirectory();
    }
    selected = id;
    const c = categoryMap.get(f.category);
    const related = (f.related || [])
      .map((id) => featureMap.get(id))
      .filter(Boolean);
    showDetail(
      `<div class="detail-top"><span style="color:${c.color}">${esc(c.title)}</span><button data-action="close" aria-label="Close feature details">×</button></div><span class="badge ${f.status}">${statuses[f.status]}</span><h2>${esc(f.title)}</h2><p>${esc(f.summary)}</p>${f.details?.length ? `<h3>How it works</h3><ul>${f.details.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}<button class="primary travel" data-travel="${id}">Fly to this feature</button>${related.length ? `<h3>Connected features</h3><div class="links">${related.map((x) => `<button data-feature="${x.id}">${esc(x.title)}</button>`).join("")}</div>` : ""}<details><summary>Status & source evidence</summary><p>${statusNotes[f.status]}</p>${(f.sources || []).map(sourceHTML).join("")}</details>`,
    );
    $("announcement").textContent =
      `Selected ${f.title}. ${statuses[f.status]}. ${f.summary}`;
    if (travel && view === "galaxy") focusPoint(galaxyMap.get(id).p, 245);
    updateHeading(c.title, f.title, f.summary);
  }
  function selectCategory(id) {
    const c = categoryMap.get(id);
    if (!c) return;
    if (view !== "galaxy") setView("galaxy");
    filterCategory = id;
    selected = id;
    renderDirectory();
    focusPoint(galaxyMap.get(id).p, 590);
    showDetail(
      `<div class="detail-top"><span style="color:${c.color}">Constellation · ${c.features.length} features</span><button data-action="close" aria-label="Close constellation details">×</button></div><h2>${esc(c.title)}</h2><p>${esc(c.summary)}</p><h3>Inside this system</h3><div class="links">${c.features.map((f) => `<button data-feature="${f.id}">${esc(f.title)} <span class="badge ${f.status}">${statuses[f.status]}</span></button>`).join("")}</div>`,
    );
    updateHeading("Explore the system", c.title, c.summary);
  }
  function updateHeading(kicker, title, description) {
    $("scene-kicker").textContent = kicker;
    $("scene-title").textContent = title;
    $("scene-description").textContent = description;
    document.querySelector(".scene-heading").classList.add("compact");
  }
  function overview() {
    filterCategory = null;
    selected = null;
    query = "";
    status = "all";
    $("search").value = "";
    $("status-filter").value = "all";
    closeDetail();
    setMode("orbit");
    desired = { distance: 1850, target: vec() };
    orbit.yaw = 0.1;
    orbit.pitch = 0.18;
    renderDirectory();
    document.querySelector(".scene-heading").classList.remove("compact");
    $("scene-kicker").textContent = "A living system, mapped";
    $("scene-title").innerHTML = "Small texts.<br>Everything connected.";
    $("scene-description").innerHTML =
      "Choose a constellation. Follow a decision.<br>Fly into the details.";
    if (view === "decision") startFlow(activeFlow.id);
  }
  function focusPoint(p, distance) {
    setMode("orbit");
    desired = { target: { ...p }, distance };
    if (!motion) {
      orbit.target = { ...p };
      orbit.distance = distance;
    }
  }
  function setView(next) {
    view = next;
    document.body.dataset.view = view;
    document
      .querySelectorAll("[data-view]")
      .forEach((b) =>
        b.setAttribute("aria-pressed", String(b.dataset.view === view)),
      );
    $("matrix").hidden = view !== "matrix";
    $("decision-selector").hidden = view !== "decision";
    document.querySelector(".scene-heading").hidden = view !== "galaxy";
    closeDetail();
    if (view === "decision") {
      startFlow(activeFlow.id);
    } else {
      points = galaxy;
      links = galaxyLinks;
      if (view === "galaxy") overview();
      else renderMatrix();
    }
  }
  function setMode(next) {
    if (next === "fly" && mode !== "fly") {
      updateCamera();
      flight.position = { ...camera };
      flight.yaw = Math.atan2(forward.x, -forward.z);
      flight.pitch = Math.asin(forward.y);
    }
    if (next === "orbit" && mode === "fly") {
      const f = vec(
        Math.sin(flight.yaw) * Math.cos(flight.pitch),
        Math.sin(flight.pitch),
        -Math.cos(flight.yaw) * Math.cos(flight.pitch),
      );
      orbit.target = add(flight.position, mul(f, 400));
      desired.target = { ...orbit.target };
      orbit.distance = desired.distance = 400;
      const d = sub(flight.position, orbit.target);
      orbit.yaw = Math.atan2(d.x, d.z);
      orbit.pitch = Math.asin(d.y / 400);
    }
    mode = next;
    keys.clear();
    $("orbit-mode").setAttribute("aria-pressed", String(mode === "orbit"));
    $("fly-mode").setAttribute("aria-pressed", String(mode === "fly"));
    $("crosshair").hidden = mode !== "fly";
    $("flight-help").innerHTML =
      mode === "fly"
        ? "Drag to look <span>•</span> W A S D to fly <span>•</span> Q / E down / up <span>•</span> Shift to boost"
        : "Drag to orbit <span>•</span> Scroll to zoom <span>•</span> Click to explore";
  }
  function updateCamera() {
    if (mode === "orbit") {
      camera = add(
        orbit.target,
        vec(
          Math.sin(orbit.yaw) * Math.cos(orbit.pitch) * orbit.distance,
          Math.sin(orbit.pitch) * orbit.distance,
          Math.cos(orbit.yaw) * Math.cos(orbit.pitch) * orbit.distance,
        ),
      );
      forward = norm(sub(orbit.target, camera));
    } else {
      camera = flight.position;
      forward = vec(
        Math.sin(flight.yaw) * Math.cos(flight.pitch),
        Math.sin(flight.pitch),
        -Math.cos(flight.yaw) * Math.cos(flight.pitch),
      );
    }
    right = norm(cross(forward, vec(0, 1, 0)));
    up = norm(cross(right, forward));
  }
  function project(p) {
    const rel = sub(p, camera),
      z = dot(rel, forward);
    if (z < 12) return null;
    const scale = (Math.min(width, height) * 1.08) / z;
    const center =
      width > 760
        ? (width +
            (document.body.classList.contains("directory-collapsed")
              ? 0
              : 220)) /
          2
        : width / 2;
    return {
      x: center + dot(rel, right) * scale,
      y: height * 0.55 - dot(rel, up) * scale,
      z,
      scale,
    };
  }
  function resize() {
    width = innerWidth;
    height = innerHeight;
    dpr = Math.min(devicePixelRatio || 1, 2);
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width = width + "px";
    canvas.style.height = height + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  function sphere(p, r, color, alpha = 1) {
    ctx.globalAlpha = alpha;
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
    ctx.fill();
  }
  function render(ts) {
    const dt = Math.min((ts - last) / 1000 || 0, 0.05);
    last = ts;
    if (!paused) time += dt;
    if (mode === "orbit") {
      const smooth = motion ? Math.min(dt * 5, 1) : 1;
      orbit.target = lerp(orbit.target, desired.target, smooth);
      orbit.distance += (desired.distance - orbit.distance) * smooth;
    } else if (!paused) {
      const speed = (keys.has("Shift") ? 1000 : 270) * dt;
      const f = vec(
          Math.sin(flight.yaw) * Math.cos(flight.pitch),
          Math.sin(flight.pitch),
          -Math.cos(flight.yaw) * Math.cos(flight.pitch),
        ),
        r = norm(cross(f, vec(0, 1, 0)));
      if (keys.has("w")) flight.position = add(flight.position, mul(f, speed));
      if (keys.has("s")) flight.position = add(flight.position, mul(f, -speed));
      if (keys.has("a")) flight.position = add(flight.position, mul(r, -speed));
      if (keys.has("d")) flight.position = add(flight.position, mul(r, speed));
      if (keys.has("q")) flight.position.y -= speed;
      if (keys.has("e")) flight.position.y += speed;
      if (keys.has("ArrowLeft")) flight.yaw -= dt;
      if (keys.has("ArrowRight")) flight.yaw += dt;
      if (keys.has("ArrowUp")) flight.pitch = Math.min(1.5, flight.pitch + dt);
      if (keys.has("ArrowDown"))
        flight.pitch = Math.max(-1.5, flight.pitch - dt);
    }
    updateCamera();
    ctx.globalAlpha = 1;
    ctx.fillStyle = "#080d18";
    ctx.fillRect(0, 0, width, height);
    const fog = ctx.createRadialGradient(
      width * 0.56,
      height * 0.5,
      0,
      width * 0.56,
      height * 0.5,
      width * 0.62,
    );
    fog.addColorStop(0, "#19354b65");
    fog.addColorStop(0.5, "#18203c38");
    fog.addColorStop(1, "#080d1800");
    ctx.fillStyle = fog;
    ctx.fillRect(0, 0, width, height);
    for (const s of stars) {
      const p = project(s.p);
      if (p && p.x > -2 && p.x < width + 2 && p.y > -2 && p.y < height + 2) {
        sphere(
          p,
          Math.min(2, s.size),
          s.size > 1.6 ? "#bcd5e6" : "#879caf",
          s.alpha * (motion ? 0.8 + 0.2 * Math.sin(time * 0.5 + s.p.x) : 0.85),
        );
      }
    }
    if (view === "matrix") {
      ctx.globalAlpha = 1;
      requestAnimationFrame(render);
      return;
    }
    const pmap = new Map();
    for (const n of points) {
      const p = project(n.p);
      if (p) pmap.set(n.id, p);
    }
    for (const [a, b] of links) {
      const pa = pmap.get(a.id),
        pb = pmap.get(b.id);
      if (!pa || !pb) continue;
      const active =
        view === "decision"
          ? path.some((id, i) => id === a.id && path[i + 1] === b.id)
          : selected === a.id || selected === b.id || filterCategory === a.id;
      let alpha = active ? 0.53 : 0.12;
      if (view === "galaxy" && b.type === "feature" && !matches(b))
        alpha = 0.025;
      ctx.globalAlpha = alpha;
      ctx.strokeStyle = active ? b.color || "#ffd23f" : "#8bb2d1";
      ctx.lineWidth = active ? 1.35 : 0.6;
      ctx.beginPath();
      ctx.moveTo(pa.x, pa.y);
      const mid = project(add(mul(add(a.p, b.p), 0.5), vec(0, 35, 0)));
      if (mid) ctx.quadraticCurveTo(mid.x, mid.y, pb.x, pb.y);
      else ctx.lineTo(pb.x, pb.y);
      ctx.stroke();
      if (active && motion) {
        const t = (time * 0.13) % 1;
        const pp = project(lerp(a.p, b.p, t));
        if (pp) sphere(pp, 1.8, b.color || "#ffd23f", 0.9);
      }
    }
    projected = points
      .map((n) => ({ n, p: pmap.get(n.id) }))
      .filter(
        (o) =>
          o.p &&
          o.p.x > -100 &&
          o.p.x < width + 100 &&
          o.p.y > -100 &&
          o.p.y < height + 100,
      )
      .sort((a, b) => b.p.z - a.p.z);
    const labelCandidates = [];
    for (const { n, p } of projected) {
      const active =
        selected === n.id || hover === n.id || decisionSelected === n.id;
      const dim = view === "galaxy" && n.type === "feature" && !matches(n);
      const r = Math.max(
        n.type === "feature" ? 2 : 4,
        Math.min(n.radius * p.scale, active ? 22 : 36),
      );
      n.hitRadius = Math.max(9, r + 5);
      const glow = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, r * 5);
      glow.addColorStop(
        0,
        n.color + (active ? "70" : n.type === "feature" ? "25" : "50"),
      );
      glow.addColorStop(1, n.color + "00");
      ctx.globalAlpha = dim ? 0.12 : 1;
      ctx.fillStyle = glow;
      ctx.beginPath();
      ctx.arc(p.x, p.y, r * 5, 0, Math.PI * 2);
      ctx.fill();
      sphere(p, r, n.color, dim ? 0.13 : n.status === "historical" ? 0.5 : 1);
      if (n.type === "root") {
        ctx.globalAlpha = 0.35;
        ctx.strokeStyle = n.color;
        ctx.lineWidth = 0.7;
        ctx.beginPath();
        ctx.ellipse(p.x, p.y, r * 2.5, r * 0.9, -0.4, 0, Math.PI * 2);
        ctx.stroke();
      }
      if (n.type === "category" || active || n.type === "decision") {
        ctx.globalAlpha = active ? 0.9 : 0.4;
        ctx.strokeStyle = n.color;
        ctx.lineWidth = 0.7;
        ctx.beginPath();
        ctx.arc(p.x, p.y, r + 5, 0, Math.PI * 2);
        ctx.stroke();
      }
      if (
        !dim &&
        (n.type !== "feature" ||
          active ||
          labels ||
          (filterCategory === n.category && p.z < 600))
      )
        labelCandidates.push({ n, p, r, active });
    }
    const boxes = [];
    labelCandidates.sort(
      (a, b) =>
        (b.active ? 100 : 0) +
        (b.n.type === "root" ? 50 : b.n.type === "category" ? 20 : 0) -
        (a.active ? 100 : 0) -
        (a.n.type === "root" ? 50 : a.n.type === "category" ? 20 : 0),
    );
    let leafLabels = 0;
    for (const { n, p, r, active } of labelCandidates) {
      if (n.type === "feature" && !active && leafLabels > (labels ? 65 : 16))
        continue;
      const fs = n.type === "root" ? 18 : n.type === "category" ? 12 : 11;
      ctx.font = `${n.type === "root" ? 600 : 400} ${fs}px "Avenir Next", "Segoe UI", sans-serif`;
      const title = n.title.length > 43 ? n.title.slice(0, 41) + "…" : n.title;
      const w = ctx.measureText(title).width,
        x = p.x - w / 2,
        y = p.y + r + 19;
      const box = { x: x - 5, y: y - fs, w: w + 10, h: fs + 9 };
      if (
        !active &&
        boxes.some(
          (b) =>
            box.x < b.x + b.w &&
            box.x + box.w > b.x &&
            box.y < b.y + b.h &&
            box.y + box.h > b.y,
        )
      )
        continue;
      if (box.x < 8 || box.x + box.w > width - 8) continue;
      boxes.push(box);
      ctx.globalAlpha = 0.83;
      ctx.fillStyle = "#080d18";
      ctx.fillRect(box.x, box.y, box.w, box.h);
      ctx.globalAlpha = active ? 1 : n.type === "feature" ? 0.8 : 0.93;
      ctx.fillStyle = active
        ? "#ffffff"
        : n.type === "root"
          ? "#ffd23f"
          : "#dae7f2";
      ctx.fillText(title, x, y);
      if (n.type === "category") {
        ctx.font = '9px "Avenir Next",sans-serif';
        ctx.fillStyle = "#8da4bc";
        ctx.fillText(
          n.features.length + " features",
          p.x - ctx.measureText(n.features.length + " features").width / 2,
          y + 14,
        );
      }
      if (n.type === "feature") leafLabels++;
    }
    ctx.globalAlpha = 1;
    $("coordinates").textContent =
      mode === "fly"
        ? `Position ${Math.round(camera.x)} / ${Math.round(camera.y)} / ${Math.round(camera.z)}`
        : `${view === "decision" ? "Decision path" : filterCategory ? categoryMap.get(filterCategory).title : "All systems"} / ${Math.round(orbit.distance)} u`;
    requestAnimationFrame(render);
  }
  function hit(x, y) {
    let best = null,
      dist = Infinity;
    for (const { n, p } of projected) {
      if (n.type === "feature" && !matches(n)) continue;
      const d = Math.hypot(x - p.x, y - p.y);
      if (d < n.hitRadius && d < dist) {
        dist = d;
        best = n;
      }
    }
    return best;
  }
  canvas.addEventListener("pointerdown", (e) => {
    if (pointer) return;
    pointer = { x: e.clientX, y: e.clientY, id: e.pointerId };
    dragged = false;
    canvas.setPointerCapture(e.pointerId);
    canvas.focus({ preventScroll: true });
  });
  canvas.addEventListener("pointermove", (e) => {
    if (pointer && pointer.id !== e.pointerId) return;
    if (pointer) {
      const dx = e.clientX - pointer.x,
        dy = e.clientY - pointer.y;
      if (Math.abs(dx) + Math.abs(dy) > 2) dragged = true;
      if (mode === "orbit") {
        orbit.yaw -= dx * 0.005;
        orbit.pitch = Math.max(-1.45, Math.min(1.45, orbit.pitch + dy * 0.005));
      } else {
        flight.yaw += dx * 0.004;
        flight.pitch = Math.max(-1.5, Math.min(1.5, flight.pitch - dy * 0.004));
      }
      pointer.x = e.clientX;
      pointer.y = e.clientY;
      $("hover-label").hidden = true;
    } else {
      const n = hit(e.clientX, e.clientY);
      hover = n?.id || null;
      canvas.style.cursor = n ? "pointer" : "grab";
      $("hover-label").hidden = !n;
      if (n) {
        $("hover-label").textContent = n.title;
        $("hover-label").style.left =
          Math.min(width - 290, e.clientX + 16) + "px";
        $("hover-label").style.top =
          Math.min(height - 60, e.clientY + 16) + "px";
      }
    }
  });
  canvas.addEventListener("pointerup", (e) => {
    if (pointer && pointer.id !== e.pointerId) return;
    if (!dragged) {
      const n = hit(e.clientX, e.clientY);
      if (n) {
        if (view === "decision") selectDecision(n.id);
        else if (n.type === "root") overview();
        else if (n.type === "category") selectCategory(n.id);
        else selectFeature(n.id);
      }
    }
    pointer = null;
  });
  canvas.addEventListener("pointercancel", () => {
    pointer = null;
  });
  canvas.addEventListener("pointerleave", () => {
    hover = null;
    $("hover-label").hidden = true;
  });
  canvas.addEventListener(
    "wheel",
    (e) => {
      e.preventDefault();
      if (mode === "orbit")
        desired.distance = Math.max(
          80,
          Math.min(5500, desired.distance * Math.exp(e.deltaY * 0.001)),
        );
      else
        flight.position = add(flight.position, mul(forward, -e.deltaY * 0.65));
    },
    { passive: false },
  );
  function startFlow(id) {
    activeFlow = model.flows.find((f) => f.id === id) || model.flows[0];
    $("decision-select").value = activeFlow.id;
    $("decision-intro").textContent = activeFlow.summary;
    path = [activeFlow.nodes[0].id];
    points = activeFlow.nodes.map((n, i) => ({
      ...n,
      type: "decision",
      radius: 10,
      color: n.end ? "#aee6a0" : "#80d7f0",
      p: vec(
        (n.column ?? i) * 245 - 400,
        (n.lane ?? 0) * -185,
        (n.depth ?? 0) * 130,
      ),
    }));
    const map = new Map(points.map((n) => [n.id, n]));
    links = points.flatMap((n) =>
      (n.choices || [])
        .filter((c) => map.has(c.next))
        .map((c) => [n, map.get(c.next)]),
    );
    const bounds = {
      min: vec(Infinity, Infinity, Infinity),
      max: vec(-Infinity, -Infinity, -Infinity),
    };
    for (const n of points)
      for (const axis of ["x", "y", "z"]) {
        bounds.min[axis] = Math.min(bounds.min[axis], n.p[axis]);
        bounds.max[axis] = Math.max(bounds.max[axis], n.p[axis]);
      }
    const center = mul(add(bounds.min, bounds.max), 0.5);
    const span = sub(bounds.max, bounds.min);
    focusPoint(center, Math.max(1150, span.x * 1.4, span.y * 2.1));
    orbit.yaw = 0.06;
    orbit.pitch = 0.1;
    selectDecision(path[0], false);
  }
  function selectDecision(id, travel = false) {
    const n = points.find((n) => n.id === id);
    if (!n) return;
    decisionSelected = id;
    selected = id;
    const visited = path.includes(id);
    showDetail(
      `<div class="detail-top"><span>Decision model · ${visited ? "On your path" : "Explore a branch"}</span><button data-action="close" aria-label="Close decision details">×</button></div><span class="badge ${n.status || "implemented"}">${n.status ? statuses[n.status] : "Model of application rules"}</span><h2>${esc(n.title)}</h2><p>${esc(n.summary)}</p>${n.end ? `<div class="decision-end">${esc(n.end)}</div>` : ""}${(n.choices || []).map((c) => `<button class="decision-choice ${path[path.indexOf(n.id) + 1] === c.next ? "selected" : ""}" data-next="${c.next}" data-from="${n.id}">${esc(c.label)} →</button>`).join("")}${
        (n.features || []).length
          ? `<h3>Related capabilities</h3><div class="links">${n.features
              .map((id) => featureMap.get(id))
              .filter(Boolean)
              .map(
                (f) =>
                  `<button data-feature="${f.id}">${esc(f.title)}</button>`,
              )
              .join("")}</div>`
          : ""
      }<p style="font-size:10px;color:var(--muted)">An explorable explanation. Choices here never schedule an event or send a text.</p>`,
    );
    if (travel) focusPoint(n.p, 750);
    $("announcement").textContent = `Decision: ${n.title}. ${n.summary}`;
  }
  function chooseDecision(from, next) {
    const idx = path.indexOf(from);
    path = idx >= 0 ? path.slice(0, idx + 1) : [from];
    path.push(next);
    selectDecision(next, true);
  }
  function tourStep(index) {
    const stops = model.tour?.length
      ? model.tour
      : model.categories.map((c) => c.id);
    tourIndex = (index + stops.length) % stops.length;
    if (view !== "galaxy") setView("galaxy");
    $("tour").hidden = false;
    $("tour-progress").textContent = `${tourIndex + 1} / ${stops.length}`;
    const id = stops[tourIndex];
    if (categoryMap.has(id)) selectCategory(id);
    else selectFeature(id);
  }
  function info(kind) {
    const control = `<h2>Your flight controls</h2><p><b>Orbit:</b> drag the open space to rotate. Scroll or use + / − to zoom. Click a star or a feature in the directory to see what it does.</p><p><b>Fly:</b> drag to look around, or use the arrow keys. Hold W / S to move forward / backward, A / D to strafe, Q / E to go down / up. Hold Shift for faster flight. Scroll also moves forward and backward.</p><p><b>Navigation:</b> press / to search, H for the overview, and Escape to close a panel. The feature matrix is the complete keyboard-accessible index. Use the directory on touch screens, plus drag and the visible + / − controls. In Fly mode those buttons move forward and backward.</p><p><b>Comfort:</b> turn Motion off for instant camera changes and a still star field. System reduced-motion preferences are respected.</p>`;
    const coverage = `<h2>The whole picture,<br>with the evidence attached.</h2><p>${esc(model.description)}</p><div class="stat-row"><div><strong>${features.length}</strong><span>feature records</span></div><div><strong>${model.categories.length}</strong><span>systems</span></div><div><strong>${model.flows.length}</strong><span>decision paths</span></div><div><strong>${model.coverage.chats.length}</strong><span>chats reviewed</span></div></div><h3>What the status labels mean</h3>${Object.entries(
      statuses,
    )
      .map(
        ([id, label]) =>
          `<p><span class="badge ${id}">${label}</span> ${statusNotes[id]}</p>`,
      )
      .join(
        "",
      )}<h3>Coverage & boundaries</h3><ul>${model.coverage.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul><p>Source snapshot: ${esc(model.date)}. Repository revision: ${esc(model.revision.slice(0, 8))}. Feature counts describe inventory records, not unique completed production capabilities.</p><h3>Chat sources</h3><div class="sources-list">${model.coverage.chats.map((s) => "<div>" + sourceHTML(s) + (s.coverage ? '<p style="font-size:10px;margin:0 0 12px">' + esc(s.coverage) + "</p>" : "") + "</div>").join("")}</div><h3>Read the implementation</h3><p>Every feature includes source evidence. Repository links use this snapshot; chat links open the corresponding Codex conversation. The explorer is entirely local and makes no application calls.</p><button class="primary" id="export-model">Download feature inventory</button>`;
    $("dialog-content").innerHTML = kind === "help" ? control : coverage;
    if (!$("info-dialog").open) $("info-dialog").showModal();
    paused = true;
    keys.clear();
  }
  document.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.feature) {
      selectFeature(b.dataset.feature);
      if (innerWidth <= 760) collapseDirectory(true);
    } else if (b.dataset.category) {
      selectCategory(b.dataset.category);
      if (innerWidth <= 760) collapseDirectory(true);
    } else if (b.dataset.travel) {
      const id = b.dataset.travel;
      if (view !== "galaxy") setView("galaxy");
      selectFeature(id, true);
    } else if (b.dataset.next) chooseDecision(b.dataset.from, b.dataset.next);
    else if (b.dataset.view) setView(b.dataset.view);
    else if (b.dataset.action === "close") closeDetail();
    else if (b.dataset.action === "all") {
      filterCategory = null;
      renderDirectory();
    }
    if (b.id === "export-model") {
      const url = URL.createObjectURL(
        new Blob([JSON.stringify(model, null, 2)], {
          type: "application/json",
        }),
      );
      const a = document.createElement("a");
      a.href = url;
      a.download = "text-monkey-feature-inventory.json";
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
  });
  function collapseDirectory(collapse) {
    document.body.classList.toggle("directory-collapsed", collapse);
    document.querySelector(".directory").hidden = collapse;
    $("open-directory").hidden = !collapse;
  }
  $("search").addEventListener("input", (e) => {
    query = e.target.value.trim().toLowerCase();
    renderDirectory();
  });
  $("status-filter").addEventListener("change", (e) => {
    status = e.target.value;
    renderDirectory();
  });
  $("clear-filters").onclick = () => {
    query = "";
    status = "all";
    filterCategory = null;
    $("search").value = "";
    $("status-filter").value = "all";
    renderDirectory();
  };
  $("collapse-directory").onclick = () => collapseDirectory(true);
  $("open-directory").onclick = () => collapseDirectory(false);
  $("home").onclick = () => setView("galaxy");
  $("zoom-in").onclick = () => {
    if (mode === "fly")
      flight.position = add(flight.position, mul(forward, 100));
    else desired.distance = Math.max(80, desired.distance * 0.75);
  };
  $("zoom-out").onclick = () => {
    if (mode === "fly")
      flight.position = add(flight.position, mul(forward, -100));
    else desired.distance = Math.min(5500, desired.distance * 1.3);
  };
  document.querySelector(".brand").onclick = (e) => {
    e.preventDefault();
    setView("galaxy");
  };
  $("orbit-mode").onclick = () => setMode("orbit");
  $("fly-mode").onclick = () => {
    if (view === "matrix") setView("galaxy");
    setMode("fly");
    canvas.focus();
  };
  $("labels").onclick = () => {
    $("labels").setAttribute("aria-pressed", String((labels = !labels)));
  };
  $("motion").onclick = () => {
    $("motion").setAttribute("aria-pressed", String((motion = !motion)));
    $("motion").textContent = motion ? "Motion on" : "Motion off";
  };
  $("motion").setAttribute("aria-pressed", String(motion));
  $("motion").textContent = motion ? "Motion on" : "Motion off";
  $("help-button").onclick = () => info("help");
  $("coverage-button").onclick = () => info("coverage");
  document.querySelector(".dialog-close").onclick = () =>
    $("info-dialog").close();
  $("info-dialog").addEventListener("close", () => {
    paused = false;
  });
  $("info-dialog").addEventListener("click", (e) => {
    if (e.target === $("info-dialog")) {
      const r = $("info-dialog").getBoundingClientRect();
      if (
        e.clientX < r.left ||
        e.clientX > r.right ||
        e.clientY < r.top ||
        e.clientY > r.bottom
      )
        $("info-dialog").close();
    }
  });
  $("fullscreen").onclick = () => {
    if (document.fullscreenElement) document.exitFullscreen?.();
    else document.documentElement.requestFullscreen?.().catch(() => {});
  };
  $("tour-button").onclick = () => tourStep(0);
  $("tour-previous").onclick = () => tourStep(tourIndex - 1);
  $("tour-next").onclick = () => tourStep(tourIndex + 1);
  $("tour-close").onclick = () => {
    $("tour").hidden = true;
    tourIndex = -1;
  };
  $("decision-select").innerHTML = model.flows
    .map((f) => `<option value="${f.id}">${esc(f.title)}</option>`)
    .join("");
  $("decision-select").onchange = (e) => startFlow(e.target.value);
  $("restart-path").onclick = () => startFlow(activeFlow.id);
  addEventListener("keydown", (e) => {
    if (e.target.matches("input,textarea,select") || $("info-dialog").open)
      return;
    const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
    if (key === "/") {
      e.preventDefault();
      collapseDirectory(false);
      $("search").focus();
      return;
    }
    if (key === "Escape") {
      closeDetail();
      $("tour").hidden = true;
      return;
    }
    if (key === "h") {
      setView("galaxy");
      return;
    }
    if (key === "+" || key === "=")
      desired.distance = Math.max(80, desired.distance * 0.85);
    if (key === "-") desired.distance = Math.min(5500, desired.distance * 1.15);
    if (
      mode === "fly" &&
      [
        "w",
        "a",
        "s",
        "d",
        "q",
        "e",
        "Shift",
        "ArrowUp",
        "ArrowDown",
        "ArrowLeft",
        "ArrowRight",
      ].includes(key)
    ) {
      e.preventDefault();
      keys.add(key);
    }
  });
  addEventListener("keyup", (e) =>
    keys.delete(e.key.length === 1 ? e.key.toLowerCase() : e.key),
  );
  addEventListener("blur", () => {
    keys.clear();
    pointer = null;
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) keys.clear();
  });
  addEventListener("resize", resize);
  resize();
  if (innerWidth <= 760) collapseDirectory(true);
  points = galaxy;
  links = galaxyLinks;
  renderDirectory();
  document.body.dataset.view = view;
  requestAnimationFrame(render);
})();
