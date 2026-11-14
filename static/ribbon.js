// Behind the story page: a blue ribbon drawn down the page as you scroll, the way a pen would.
// Each section gets one gesture (in from an edge, a lasso round a box or an arc across a heading, out the other
// edge); between them it runs off screen, so it is one ribbon that grows as you scroll and pulls back as you go up.
// It bends away from the pointer; over it, a clear liquid surface ripples wherever the pointer moves.
import * as THREE from 'three';

// One gesture per section: in from one edge, one round lasso around a box, out the other edge.
// fx/fy place the lasso's centre on the box; `sweep` draws a single arc across it instead. Boxes hidden on this
// layout (zero width) are skipped. Between gestures the route runs off screen, so it stays one continuous ribbon.
const LOOPS = [
  { sel: '.statement .split > :first-child', fx: 0.32, fy: 0.55 },  // the empty column under the heading
  { sel: '#how h2', sweep: true },                                    // the sticky stage below leaves no room for a loop
  { sel: '#bigstats > :first-child', fx: 0.45, fy: 0.05 },           // the 100% card
];
// One column (the page's CSS stacks everything at 860 px and below): no free room for a lasso beside the copy,
// so just one arc across each heading.
const LOOPS_NARROW = [{ sel: '#how h2', sweep: true }, { sel: '#results h2', sweep: true }];
const RADIAL = 16, SEGMENTS_PER_SCREEN = 320, MAX_SEGMENTS = 6000;
const HEAD_AT = 0.6;       // heading down the page, the head stays this far down the viewport
const LOOP_SCROLL = 0.12;  // least px of scroll per px of ribbon: a lasso is one quick sweep
const HEAD_TOP = 0.35;     // and no point is drawn higher than this: a lasso is thrown low and closes in view
const RISE = 0.8;          // a gesture starts this many viewport heights above its lasso
const EDGE = 0.62;         // the off-screen runs, as a fraction of the width from the centre
const DEPTH = 300;         // px of depth in a loop, so its far side sits behind its near side
const FAR = 0x2b3dfb, NEAR = 0x4f6bfa;  // flat blue, a little lighter where the strand is nearer
const PUSH_RADIUS = 150, MAX_PUSH = 70;
const EASE_SCROLL = 0.08, PUSH_DECAY = 0.92;

const loopRadius = (w, h) => Math.min(0.14 * w, 0.22 * h);

// One round lasso, in page px. On the left half it runs clockwise on screen: down the side facing the middle,
// round the bottom, up the outside, over the top, crossing its own entry strand. Mirrored on the right.
function loopPoints(cx, cy, r, side) {
  const points = [];
  for (let t = Math.PI / 4; t >= -2 * Math.PI - 1e-6; t -= Math.PI / 4) {
    const turns = (Math.PI / 4 - t) / (2 * Math.PI), grow = 1 + 0.2 * turns;
    points.push(new THREE.Vector3(cx - side * r * grow * Math.cos(t), cy - 0.25 * r * turns + r * grow * Math.sin(t),
      (-0.5 * Math.sin(t) + turns) * DEPTH));
  }
  return points;
}

function routePoints(w, h) {
  const narrow = w <= 860, r = loopRadius(w, h), edge = EDGE * w, rise = RISE * h;
  const pageY = (y) => -(y + window.scrollY), points = [];
  for (const { sel, fx, fy, sweep } of narrow ? LOOPS_NARROW : LOOPS) {
    const b = document.querySelector(sel)?.getBoundingClientRect();
    if (!b?.width) continue;
    if (sweep) {  // in from the side the route is on, across the heading, out the other side
      const s = points.length && points[points.length - 1].x < 0 ? -1 : 1;
      points.push(new THREE.Vector3(s * edge, pageY(b.top - (narrow ? 0.2 : 0.4) * h), 0),
        new THREE.Vector3(0, pageY(b.top + 0.5 * b.height), 0),
        new THREE.Vector3(-s * edge, pageY(b.bottom + (narrow ? 0 : 0.2) * h), 0));
      continue;
    }
    const cx = b.left + fx * b.width - w / 2, cy = pageY(b.top + fy * b.height), side = cx < 0 ? -1 : 1;
    points.push(new THREE.Vector3(side * edge, cy + rise, 0), new THREE.Vector3(cx - side * 0.2 * r, cy + 0.7 * rise, 0),
      ...loopPoints(cx, cy, r, side),
      new THREE.Vector3(cx - side * 1.25 * r, cy - r, DEPTH), new THREE.Vector3(-side * edge, cy - 1.15 * r, DEPTH));
  }
  return points.length > 1 ? points : null;
}

export function startRibbon(canvas) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  const liquid = makeLiquid(renderer);
  const scene = new THREE.Scene();
  const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 5000);
  camera.position.z = 2000;

  const uniforms = { uPointer: { value: new THREE.Vector2(1e5, 1e5) }, uPush: { value: 0 }, uRadius: { value: 0 },
    uFar: { value: new THREE.Color(FAR) }, uNear: { value: new THREE.Color(NEAR) } };
  const material = new THREE.MeshBasicMaterial();
  material.onBeforeCompile = (shader) => {
    Object.assign(shader.uniforms, uniforms);
    const decl = '#include <common>\nvarying float vDepth;\nuniform vec2 uPointer;\nuniform float uPush;\nuniform float uRadius;\nuniform vec3 uFar;\nuniform vec3 uNear;';
    shader.vertexShader = shader.vertexShader
      .replace('#include <common>', decl)
      .replace('#include <begin_vertex>', `#include <begin_vertex>
        vec2 world = (modelMatrix * vec4(transformed - normal * uRadius, 1.0)).xy;  // the ring's centre: whole rings move
        vec2 away = world - uPointer;
        float near = exp(-dot(away, away) / ${(PUSH_RADIUS ** 2).toFixed(1)});
        transformed.xy += normalize(away + 1e-4) * near * uPush;
        vDepth = (modelMatrix * vec4(transformed, 1.0)).z;`);
    shader.fragmentShader = shader.fragmentShader
      .replace('#include <common>', decl)
      .replace('vec4 diffuseColor = vec4( diffuse, opacity );',
        `vec4 diffuseColor = vec4(mix(uFar, uNear, smoothstep(${-0.5 * DEPTH}.0, ${1.2 * DEPTH}.0, vDepth)), opacity);`);
  };

  const cap = new THREE.Mesh(new THREE.BufferGeometry(), material);
  scene.add(cap);

  let tube = null, route = null, reveal = new Float32Array(1), segments = 1, h = 1, pageH = 0, builtW = 0, builtH = 0;
  function fit() {
    const w = window.innerWidth;
    h = window.innerHeight;
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.setSize(w, h, false);
    liquid?.resize(w, h);
    Object.assign(camera, { left: -w / 2, right: w / 2, top: h / 2, bottom: -h / 2 });
    camera.updateProjectionMatrix();
  }
  function build() {
    fit();
    const w = (builtW = window.innerWidth);
    builtH = h;
    pageH = document.documentElement.scrollHeight;
    const points = routePoints(w, h);
    if (!points) return;
    const radius = Math.max(8, Math.min(20, w * 0.0125));
    uniforms.uRadius.value = radius;
    segments = Math.min(MAX_SEGMENTS, Math.max(400, Math.round((pageH / h) * SEGMENTS_PER_SCREEN)));
    route = new THREE.CatmullRomCurve3(points, false, 'centripetal');
    route.arcLengthDivisions = segments * 2;
    const gap = LOOP_SCROLL * (route.getLength() / segments);  // least scroll between two drawn points
    const depth = new Float32Array(segments + 1), latest = new Float32Array(segments + 1);
    for (let i = 0; i <= segments; i++) depth[i] = -route.getPointAt(i / segments).y;
    for (let i = segments, r = Infinity; i >= 0; i--) latest[i] = r = Math.min(r - gap, depth[i] - HEAD_TOP * h);
    reveal = new Float32Array(segments + 1);
    for (let i = 0, r = -Infinity; i <= segments; i++) reveal[i] = r = Math.min(latest[i], Math.max(r + gap, depth[i] - HEAD_AT * h));
    const geometry = new THREE.TubeGeometry(route, segments, radius, RADIAL, false);
    if (tube) { scene.remove(tube); tube.geometry.dispose(); }
    tube = new THREE.Mesh(geometry, material);
    scene.add(tube);
    cap.geometry.dispose();
    cap.geometry = new THREE.SphereGeometry(radius, 24, 16);
  }
  build();
  let rebuildTimer = 0;
  const rebuildSoon = () => { clearTimeout(rebuildTimer); rebuildTimer = setTimeout(build, 150); };
  window.addEventListener('resize', () => {
    fit();
    if (window.innerWidth !== builtW || Math.abs(window.innerHeight - builtH) > 0.2 * builtH) rebuildSoon();
  });
  new ResizeObserver(() => { if (Math.abs(document.documentElement.scrollHeight - pageH) > 1) rebuildSoon(); }).observe(document.body);

  const headFor = (scrollY) => {
    let lo = 0, hi = segments + 1;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (reveal[mid] <= scrollY) lo = mid + 1; else hi = mid; }
    return Math.max(0, lo - 1) / segments;
  };

  let progress = 0, push = 0, last = null, prevNow = 0;
  const client = new THREE.Vector2(-1, -1);
  const pointerUv = new THREE.Vector2(-1, -1), frameUv = new THREE.Vector2(-1, -1);
  window.addEventListener('pointerdown', (e) => { last = new THREE.Vector2(e.clientX, e.clientY); }, { passive: true });
  window.addEventListener('pointermove', (e) => {
    const p = new THREE.Vector2(e.clientX, e.clientY);
    if (last) push = Math.min(MAX_PUSH, push + p.distanceTo(last) * 0.6);
    last = p;
    client.copy(p);
    pointerUv.set(e.clientX / window.innerWidth, 1 - e.clientY / window.innerHeight);
  }, { passive: true });

  function frame(now) {
    requestAnimationFrame(frame);
    if (document.hidden) return;
    const dt = Math.min(0.1, (now - (prevNow || now)) / 1000);
    prevNow = now;
    const scrollY = window.scrollY;
    camera.position.y = -scrollY - h / 2;
    push *= PUSH_DECAY;
    uniforms.uPointer.value.set(client.x - window.innerWidth / 2, -(scrollY + client.y));
    uniforms.uPush.value = push;
    if (tube) {
      progress += (headFor(scrollY) - progress) * (1 - Math.pow(1 - EASE_SCROLL, dt * 60));
      const end = Math.round(progress * segments);
      tube.geometry.setDrawRange(0, end * RADIAL * 6);
      cap.visible = end > 0;
      route.getPointAt(end / segments, cap.position);
    }
    const movedPx = pointerUv.distanceTo(frameUv) * window.innerWidth;
    liquid?.step(frameUv, pointerUv, frameUv.x < 0 ? 0 : Math.min(LIQUID.maxPush, movedPx * LIQUID.pushPerPx));
    frameUv.copy(pointerUv);
    renderer.render(scene, camera);
    liquid?.draw();
  }
  requestAnimationFrame(frame);
}

const SIM_SCALE = 4;   // the liquid is simulated at a quarter of the screen's resolution
const LIQUID = { damping: 0.965, settle: 0.992, radius: 0.018, pushPerPx: 0.003, maxPush: 0.18, slope: 7.0 };  // settle: the surface flattens again

const QUAD_VERTEX = 'varying vec2 vUv;\nvoid main() { vUv = uv; gl_Position = vec4(position.xy, 0.0, 1.0); }';

// One step of a damped wave equation on a height field (r: height, g: velocity). The pointer presses the
// surface down along the path it moved this frame, and the dent spreads out as ripples.
const SIM_FRAGMENT = `
  uniform sampler2D uState; uniform vec2 uTexel; uniform vec2 uFrom; uniform vec2 uTo;
  uniform float uPush; uniform float uAspect; uniform float uDamping; uniform float uSettle; uniform float uRadius;
  varying vec2 vUv;
  float toSegment(vec2 p, vec2 a, vec2 b) {
    vec2 pa = p - a, ba = b - a;
    return length(pa - ba * clamp(dot(pa, ba) / max(dot(ba, ba), 1e-8), 0.0, 1.0));
  }
  void main() {
    vec4 s = texture2D(uState, vUv);
    float around = (texture2D(uState, vUv + vec2(uTexel.x, 0.0)).r + texture2D(uState, vUv - vec2(uTexel.x, 0.0)).r
                  + texture2D(uState, vUv + vec2(0.0, uTexel.y)).r + texture2D(uState, vUv - vec2(0.0, uTexel.y)).r) * 0.25;
    float velocity = (s.g + (around - s.r) * 2.0) * uDamping;
    float height = (s.r + velocity) * uSettle;
    vec2 aspect = vec2(uAspect, 1.0);
    float d = toSegment(vUv * aspect, uFrom * aspect, uTo * aspect);
    height -= uPush * exp(-d * d / (uRadius * uRadius));
    gl_FragColor = vec4(height, velocity, 0.0, 1.0);
  }`;

// The liquid itself is clear: only its light shows, a highlight where the surface tilts toward the light
// and a faint shadow where it tilts away. A flat surface draws nothing.
const SURFACE_FRAGMENT = `
  uniform sampler2D uState; uniform vec2 uTexel; uniform float uSlope;
  varying vec2 vUv;
  void main() {
    float dx = texture2D(uState, vUv - vec2(uTexel.x, 0.0)).r - texture2D(uState, vUv + vec2(uTexel.x, 0.0)).r;
    float dy = texture2D(uState, vUv - vec2(0.0, uTexel.y)).r - texture2D(uState, vUv + vec2(0.0, uTexel.y)).r;
    vec3 normal = normalize(vec3(dx * uSlope, dy * uSlope, 1.0));
    vec3 light = normalize(vec3(-0.5, 0.7, 0.6));
    float facing = dot(normal.xy, light.xy);
    float shine = pow(max(dot(reflect(vec3(0.0, 0.0, -1.0), normal), light), 0.0), 18.0);
    float lit = clamp(facing * 4.0, 0.0, 1.0), dark = clamp(-facing * 4.0, 0.0, 1.0);
    vec3 color = mix(vec3(0.16, 0.18, 0.28), vec3(1.0), step(0.0, facing));
    float alpha = max(lit * 0.6, dark * 0.2) + shine * 0.55;
    gl_FragColor = vec4(mix(color, vec3(1.0), clamp(shine, 0.0, 1.0)), clamp(alpha, 0.0, 0.85));
  }`;

/** A clear liquid surface over the whole screen that the pointer stirs; null where float targets are missing. */
function makeLiquid(renderer) {
  if (!renderer.extensions.has('EXT_color_buffer_float') && !renderer.extensions.has('EXT_color_buffer_half_float')) return null;
  const target = () => new THREE.WebGLRenderTarget(1, 1, { type: THREE.HalfFloatType, minFilter: THREE.LinearFilter, magFilter: THREE.LinearFilter, depthBuffer: false });
  let read = target(), write = target();
  const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);
  const quad = new THREE.PlaneGeometry(2, 2);
  const sim = new THREE.ShaderMaterial({ vertexShader: QUAD_VERTEX, fragmentShader: SIM_FRAGMENT, uniforms: {
    uState: { value: null }, uTexel: { value: new THREE.Vector2() }, uFrom: { value: new THREE.Vector2() }, uTo: { value: new THREE.Vector2() },
    uPush: { value: 0 }, uAspect: { value: 1 }, uDamping: { value: LIQUID.damping }, uSettle: { value: LIQUID.settle }, uRadius: { value: LIQUID.radius } } });
  const surface = new THREE.ShaderMaterial({ vertexShader: QUAD_VERTEX, fragmentShader: SURFACE_FRAGMENT, transparent: true, depthTest: false, depthWrite: false,
    toneMapped: false, uniforms: { uState: { value: null }, uTexel: { value: new THREE.Vector2() }, uSlope: { value: LIQUID.slope } } });
  const simScene = new THREE.Scene(), surfaceScene = new THREE.Scene();
  simScene.add(new THREE.Mesh(quad, sim));
  surfaceScene.add(new THREE.Mesh(quad, surface));
  return {
    resize(w, h) {
      const sw = Math.ceil(w / SIM_SCALE), sh = Math.ceil(h / SIM_SCALE);
      read.setSize(sw, sh);
      write.setSize(sw, sh);
      sim.uniforms.uTexel.value.set(1 / sw, 1 / sh);
      surface.uniforms.uTexel.value.set(1 / sw, 1 / sh);
      sim.uniforms.uAspect.value = w / h;
    },
    step(from, to, push) {
      Object.assign(sim.uniforms.uState, { value: read.texture });
      sim.uniforms.uFrom.value.copy(from);
      sim.uniforms.uTo.value.copy(to);
      sim.uniforms.uPush.value = push;
      renderer.setRenderTarget(write);
      renderer.render(simScene, camera);
      renderer.setRenderTarget(null);
      [read, write] = [write, read];
    },
    draw() {
      surface.uniforms.uState.value = read.texture;
      renderer.autoClear = false;
      renderer.render(surfaceScene, camera);
      renderer.autoClear = true;
    },
  };
}
