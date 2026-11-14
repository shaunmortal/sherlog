// The story page's hero: the shop's call graph in 3D, with requests flowing along every call.
// createShop3D(host, meta, pos, paused) -> { setLevel(service, 'ok'|'warn'|'bad'), tag(service, tags), pause(), play() }
// Paused (reduced motion, or the page's pause button) it draws only when something changes.
import * as THREE from 'three';
import { CSS2DRenderer, CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';

const PX_PER_UNIT = 68;               // the 2D map's layout, 68 px to a unit
const FOV = 32;
const COLOR = { light: 0xe9ebf5, dark: 0x2a2d3a, bad: 0xff4c41, blue: 0x1a2ffb, wire: 0x3b4054, flow: 0x9aa5ff };
const DARK_SERVICES = new Set(['db', 'cache', 'payment', 'currency']);  // the data layer is darker
const FLASH_MS = 900;
const PULSES_PER_CALL = 2;
const BAD = new THREE.Color(COLOR.bad);

export function createShop3D(host, meta, pos, paused) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  host.append(renderer.domElement);
  const labels = new CSS2DRenderer();
  labels.domElement.className = 'labels3d';
  host.append(labels.domElement);

  const scene = new THREE.Scene();
  scene.environment = new THREE.PMREMGenerator(renderer).fromScene(new RoomEnvironment(), 0.04).texture;
  scene.add(new THREE.AmbientLight(0xffffff, 0.3));
  const key = new THREE.DirectionalLight(0xffffff, 1.4);
  key.position.set(4, 6, 8);
  scene.add(key);
  const camera = new THREE.PerspectiveCamera(FOV, 1, 0.1, 100);
  const graph = new THREE.Group();
  scene.add(graph);

  const at = (s, i) => new THREE.Vector3((pos[s][0] - 400) / PX_PER_UNIT, -(pos[s][1] - 170) / PX_PER_UNIT, (((i * 37) % 7) - 3) * 0.32);
  const nodes = {};
  const boxGeometry = new RoundedBoxGeometry(1.3, 0.52, 0.52, 4, 0.22);
  meta.services.forEach((s, i) => {
    const base = new THREE.Color(DARK_SERVICES.has(s) ? COLOR.dark : COLOR.light);
    const material = new THREE.MeshPhysicalMaterial({ color: base.clone(), roughness: 0.3, clearcoat: 1, clearcoatRoughness: 0.12, emissive: COLOR.bad, emissiveIntensity: 0 });
    const mesh = new THREE.Mesh(boxGeometry, material);
    mesh.position.copy(at(s, i));
    const ring = new THREE.Mesh(new THREE.RingGeometry(0.62, 0.68, 64), new THREE.MeshBasicMaterial({ color: COLOR.bad, transparent: true, opacity: 0, side: THREE.DoubleSide }));
    const halo = new THREE.Mesh(new THREE.TorusGeometry(1.0, 0.035, 12, 80), new THREE.MeshBasicMaterial({ color: COLOR.blue }));
    halo.visible = false;
    mesh.add(ring, halo);
    const label = document.createElement('div');
    label.className = 'lbl3d';
    const name = Object.assign(document.createElement('span'), { className: 'name', textContent: s });
    const tags = document.createElement('span');
    tags.className = 'tags3d';
    label.append(name, tags);
    const tag = new CSS2DObject(label);
    tag.position.set(0, -0.52, 0);
    mesh.add(tag);
    graph.add(mesh);
    nodes[s] = { mesh, material, base, ring, halo, tags, level: 'ok', flashAt: -Infinity };
  });

  const calls = [];
  for (const [caller, callees] of Object.entries(meta.calls)) {
    for (const callee of Object.keys(callees)) {
      const a = nodes[caller].mesh.position, b = nodes[callee].mesh.position;
      const wire = new THREE.Mesh(new THREE.TubeGeometry(new THREE.LineCurve3(a, b), 1, 0.018, 6), new THREE.MeshBasicMaterial({ color: COLOR.wire }));
      const pulses = Array.from({ length: PULSES_PER_CALL }, () => new THREE.Mesh(new THREE.SphereGeometry(0.06, 12, 12), new THREE.MeshBasicMaterial({ color: COLOR.flow })));
      graph.add(wire, ...pulses);
      calls.push({ a, b, callee, wire, pulses, offset: Math.random() });
    }
  }

  // pointer parallax, fitted camera, and only rendering while the hero is on screen
  let pointer = { x: 0, y: 0 }, onScreen = true, looping = false, t = 0;
  host.addEventListener('pointermove', (e) => {
    const box = host.getBoundingClientRect();
    pointer = { x: (e.clientX - box.left) / box.width - 0.5, y: (e.clientY - box.top) / box.height - 0.5 };
  });
  host.addEventListener('pointerleave', () => { pointer = { x: 0, y: 0 }; });
  const fit = () => {
    const { width, height } = host.getBoundingClientRect();
    if (!width || !height) return;
    renderer.setSize(width, height);
    labels.setSize(width, height);
    camera.aspect = width / height;
    const tan = Math.tan(THREE.MathUtils.degToRad(FOV / 2));
    camera.position.set(0, 0, Math.max(3.4 / tan, 6.5 / (tan * camera.aspect)));
    camera.updateProjectionMatrix();
    draw(performance.now());
  };
  new ResizeObserver(fit).observe(host);
  new IntersectionObserver(([entry]) => { onScreen = entry.isIntersecting; }).observe(host);

  function draw(now) {
    if (!paused) t = now / 1000;  // paused, the scene's clock stands still, so nothing sways, spins or pulses
    graph.rotation.y += (Math.sin(t * 0.25) * 0.28 + pointer.x * 0.5 - graph.rotation.y) * 0.05;
    graph.rotation.x += (0.06 + pointer.y * 0.25 - graph.rotation.x) * 0.05;
    for (const n of Object.values(nodes)) {
      const sick = n.level !== 'ok';
      n.material.color.lerp(sick ? BAD : n.base, 0.08);
      n.material.emissiveIntensity = sick ? 0.25 + 0.15 * Math.sin(t * 4) : 0;
      const age = (now - n.flashAt) / FLASH_MS;
      n.ring.material.opacity = age < 1 ? 0.8 * (1 - age) : 0;
      n.ring.scale.setScalar(age < 1 ? 1 + age * 1.6 : 1);
      n.halo.rotation.set(Math.PI / 2 + Math.sin(t) * 0.3, t * 0.8, 0);
    }
    for (const c of calls) {
      const sick = nodes[c.callee].level !== 'ok';  // failures travel back up to the callers
      c.wire.material.color.set(sick ? COLOR.bad : COLOR.wire);
      c.pulses.forEach((p, k) => {
        const f = (t * (sick ? 0.9 : 0.35) + c.offset + k / PULSES_PER_CALL) % 1;
        p.position.lerpVectors(sick ? c.b : c.a, sick ? c.a : c.b, f);
        p.material.color.set(sick ? COLOR.bad : COLOR.flow);
      });
    }
    renderer.render(scene, camera);
    labels.render(scene, camera);
  }
  const loop = (now) => {
    if (paused) { looping = false; return; }  // no frames at all until play()
    if (onScreen && !document.hidden) draw(now);
    requestAnimationFrame(loop);
  };
  const run = () => { if (!looping) { looping = true; requestAnimationFrame(loop); } };
  if (!paused) run();
  fit();

  return {
    setLevel(service, level) {
      const n = nodes[service];
      if (level !== 'ok' && n.level === 'ok') n.flashAt = performance.now();
      n.level = level;
      if (paused) { n.material.color.set(level === 'ok' ? n.base : COLOR.bad); draw(performance.now()); }
    },
    tag(service, tags) {
      const n = nodes[service];
      n.halo.visible = tags.length > 0;
      n.tags.replaceChildren(...tags.map(([cls, text]) => Object.assign(document.createElement('b'), { className: cls, textContent: text })));
      if (paused) draw(performance.now());
    },
    pause() { paused = true; },
    play() { paused = false; run(); },
  };
}
