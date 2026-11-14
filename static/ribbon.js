// Behind the story page: a glossy ribbon laid down the page as you scroll. It grows from the top of the page
// to where you are reading, shrinks back when you scroll up, and bends away from the pointer; over it, a
// clear liquid surface ripples wherever the pointer moves.
import * as THREE from 'three';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';

const RADIAL = 18, SEGMENTS_PER_SCREEN = 260, MAX_SEGMENTS = 6000;
const BEND_EVERY = 0.6;                    // viewport heights between the ribbon's bends down the page
const HEAD_AT = 0.6;                       // the head is drawn this far down the viewport
const COLOR_BAND = 1.5;                    // viewport heights of ribbon per cyan-blue-cyan cycle
const DEPTH_PX = 400;
const CYAN = new THREE.Color(0x8fe3f2), BLUE = new THREE.Color(0x1a2ffb);
const PUSH_RADIUS = 150, MAX_PUSH = 70;    // px: how far the pointer reaches and how hard it pushes
const EASE_SCROLL = 0.07, PUSH_DECAY = 0.92;

const hash = (i) => { const s = Math.sin(i * 12.9898) * 43758.5453; return s - Math.floor(s); };  // stable 0..1 per bend

// The route in page pixels: x from the viewport's centre, y = -(distance down the page). It enters from off
// screen on the left and swings side to side, staying on screen so the head is always in view, to the bottom.
function routePoints(w, h, pageH) {
  const points = [new THREE.Vector3(-0.75 * w, -0.45 * h, 0)];
  for (let i = 1, y = 0.45 * h; y < pageH; i++) {
    y += BEND_EVERY * h * (0.8 + 0.4 * hash(i + 3));
    const side = i % 2 ? 1 : -1;
    points.push(new THREE.Vector3(side * (0.35 + 0.5 * hash(i)) * (w / 2), -y, (hash(i + 7) - 0.5) * 0.7 * DEPTH_PX));
  }
  return points;
}

export function startRibbon(canvas) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  const liquid = makeLiquid(renderer);
  const scene = new THREE.Scene();
  scene.environment = new THREE.PMREMGenerator(renderer).fromScene(new RoomEnvironment(), 0.04).texture;
  const sun = new THREE.DirectionalLight(0xffffff, 1.2);
  sun.position.set(0.3, 1, 1);
  scene.add(sun, new THREE.HemisphereLight(0xffffff, 0xb8c4ff, 1.1));  // keeps the underside from going black
  const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 5000);
  camera.position.z = 2000;

  // the pointer pushes the ribbon aside in the vertex shader, and the ribbon sways a little on its own
  const uniforms = { uPointer: { value: new THREE.Vector2(1e5, 1e5) }, uPush: { value: 0 }, uTime: { value: 0 } };
  const material = new THREE.MeshPhysicalMaterial({ vertexColors: true, roughness: 0.42, clearcoat: 1, clearcoatRoughness: 0.2 });
  material.onBeforeCompile = (shader) => {
    Object.assign(shader.uniforms, uniforms);
    shader.vertexShader = shader.vertexShader
      .replace('#include <common>', '#include <common>\nuniform vec2 uPointer;\nuniform float uPush;\nuniform float uTime;')
      .replace('#include <begin_vertex>', `#include <begin_vertex>
        vec2 world = (modelMatrix * vec4(transformed, 1.0)).xy;
        vec2 away = world - uPointer;
        float near = exp(-dot(away, away) / ${(PUSH_RADIUS ** 2).toFixed(1)});
        transformed.xy += normalize(away + 1e-4) * near * uPush;
        transformed.y += sin(world.x * 0.005 + uTime * 0.8) * 10.0;`);
  };

  // the round head; the tail starts off screen, so it needs no cap. Built at full size (never scaled) so the
  // shader's pixel offsets match the tube's.
  const cap = new THREE.Mesh(new THREE.BufferGeometry(), material);
  scene.add(cap);
  const capColor = new THREE.Color();
  const paintCap = (u) => {
    shade(u, capColor);
    const attr = cap.geometry.attributes.color;
    for (let k = 0; k < attr.count; k++) attr.setXYZ(k, capColor.r, capColor.g, capColor.b);
    attr.needsUpdate = true;
  };

  let tube = null, route = null, ys = [], segments = 1, bands = 1, h = 1, pageH = 0;
  const shade = (u, out) => out.copy(CYAN).lerp(BLUE, 0.5 - 0.5 * Math.cos(2 * Math.PI * u * bands));
  function build() {
    const w = window.innerWidth;
    h = window.innerHeight;
    pageH = document.documentElement.scrollHeight;
    renderer.setSize(w, h, false);
    liquid?.resize(w, h);
    Object.assign(camera, { left: -w / 2, right: w / 2, top: h / 2, bottom: -h / 2 });
    camera.updateProjectionMatrix();
    const radius = Math.max(12, Math.min(26, w * 0.016));
    segments = Math.min(MAX_SEGMENTS, Math.max(400, Math.round((pageH / h) * SEGMENTS_PER_SCREEN)));
    route = new THREE.CatmullRomCurve3(routePoints(w, h, pageH), false, 'centripetal');
    route.arcLengthDivisions = segments * 2;  // even spacing along a long route
    bands = route.getLength() / (COLOR_BAND * h);
    ys = Array.from({ length: segments + 1 }, (_, i) => route.getPointAt(i / segments).y);
    const geometry = new THREE.TubeGeometry(route, segments, radius, RADIAL, false);
    const count = geometry.attributes.position.count, colors = new Float32Array(count * 3), color = new THREE.Color();
    for (let k = 0; k < count; k++) {
      shade(Math.floor(k / (RADIAL + 1)) / segments, color);
      colors.set([color.r, color.g, color.b], k * 3);
    }
    geometry.setAttribute('color', new THREE.BufferAttribute(colors, 3));
    if (tube) { scene.remove(tube); tube.geometry.dispose(); }
    tube = new THREE.Mesh(geometry, material);
    scene.add(tube);
    const sphere = new THREE.SphereGeometry(radius, 24, 16);
    sphere.setAttribute('color', new THREE.BufferAttribute(new Float32Array(sphere.attributes.position.count * 3), 3));
    cap.geometry.dispose();
    cap.geometry = sphere;
  }
  build();
  let rebuildTimer = 0;
  const rebuildSoon = () => { clearTimeout(rebuildTimer); rebuildTimer = setTimeout(build, 150); };
  window.addEventListener('resize', rebuildSoon);
  // content that loads later makes the page longer: lay the route down to the new bottom
  new ResizeObserver(() => { if (Math.abs(document.documentElement.scrollHeight - pageH) > 1) rebuildSoon(); }).observe(document.body);

  // how far along the route the head should be: the first point below the target line on the page
  const headFor = (scrollY) => {
    const target = -(scrollY + HEAD_AT * h);
    const i = ys.findIndex((y) => y <= target);
    return (i < 0 ? segments : i) / segments;
  };

  let progress = 0, push = 0, last = null;
  const client = new THREE.Vector2(-1, -1);  // pointer in viewport px
  const pointerUv = new THREE.Vector2(-1, -1), frameUv = new THREE.Vector2(-1, -1);  // where the liquid was stirred, 0..1
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
    const scrollY = window.scrollY;
    camera.position.y = -scrollY - h / 2;  // the camera rides the scroll, so the ribbon stays on the page
    progress += (headFor(scrollY) - progress) * EASE_SCROLL;
    push *= PUSH_DECAY;
    uniforms.uPointer.value.set(client.x - window.innerWidth / 2, -(scrollY + client.y));
    uniforms.uPush.value = push;
    uniforms.uTime.value = now / 1000;
    const end = Math.ceil(progress * segments);
    tube.geometry.setDrawRange(0, end * RADIAL * 6);
    cap.visible = end > 0;
    cap.position.copy(route.getPointAt(Math.min(1, end / segments)));
    paintCap(end / segments);
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
