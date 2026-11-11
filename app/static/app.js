import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

window.__VIEWER_READY__ = true;

/* ------------------------------------------------------------------ *
 * 常量与状态
 * ------------------------------------------------------------------ */

const PALETTE = [
  '#4d8dff', '#23d3a2', '#ffb020', '#ff6b8b', '#a78bfa',
  '#38bdf8', '#f472b6', '#84cc16', '#fb923c', '#2dd4bf',
  '#c084fc', '#facc15',
];

const PRESET_DEFAULTS = {
  '常温': { temp_min: 15, temp_max: 25, goods_ratio: 1.0, blue_ice: 0, dry_ice: 0, enabled: false },
  '冷藏': { temp_min: 2, temp_max: 8, goods_ratio: 0.75, blue_ice: 0.25, dry_ice: 0, enabled: true },
  '冷冻': { temp_min: -20, temp_max: -10, goods_ratio: 0.75, blue_ice: 0, dry_ice: 0.25, enabled: true },
  '深冷': { temp_min: -80, temp_max: -60, goods_ratio: 0.7, blue_ice: 0, dry_ice: 0.3, enabled: true },
};

const state = {
  data: null,
  selected: -1,
  candidate: 0,
  layerMax: 0,
  layerShow: 0,
  explode: 0,
  showCarton: true,
  autoRotate: false,
  presets: JSON.parse(JSON.stringify(PRESET_DEFAULTS)),
  files: { boxes: null, orders: null },
};

const COOLANT_COLORS = { blue_ice: '#4fc3ff', dry_ice: '#d6e8ff' };

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ *
 * Three.js 基础场景
 * ------------------------------------------------------------------ */

const viewport = $('viewport');

const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
renderer.outputColorSpace = THREE.SRGBColorSpace;
viewport.appendChild(renderer.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color('#0b1118');

const camera = new THREE.PerspectiveCamera(45, 1, 1, 2000000);
camera.position.set(1400, 1000, 1600);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.maxPolarAngle = Math.PI * 0.495;
controls.autoRotateSpeed = 0.8;

scene.add(new THREE.HemisphereLight(0xffffff, 0x1a2a3a, 1.35));

const keyLight = new THREE.DirectionalLight(0xffffff, 1.9);
keyLight.position.set(900, 1400, 1100);
scene.add(keyLight);

const fillLight = new THREE.DirectionalLight(0x9fc2ff, 0.55);
fillLight.position.set(-1000, 700, -900);
scene.add(fillLight);

const grid = new THREE.GridHelper(6000, 60, 0x2a3c52, 0x18242f);
grid.position.y = -0.5;
scene.add(grid);

const root = new THREE.Group();
scene.add(root);

let itemMeshes = [];
let cartonShells = [];
let skuColors = new Map();

const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2(-10, -10);

/* ------------------------------------------------------------------ *
 * 渲染循环
 * ------------------------------------------------------------------ */

function resize() {
  const w = viewport.clientWidth || 1;
  const h = viewport.clientHeight || 1;
  renderer.setSize(w, h);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}

new ResizeObserver(resize).observe(viewport);
resize();

function animate() {
  requestAnimationFrame(animate);
  controls.autoRotate = state.autoRotate;
  controls.update();
  renderer.render(scene, camera);
}
animate();

/* ------------------------------------------------------------------ *
 * 场景构建
 * ------------------------------------------------------------------ */

function disposeGroup(group) {
  group.traverse((obj) => {
    if (obj.geometry) obj.geometry.dispose();
    if (obj.material) {
      if (Array.isArray(obj.material)) obj.material.forEach((m) => m.dispose());
      else obj.material.dispose();
    }
  });
  group.clear();
}

function clearScene() {
  disposeGroup(root);
  itemMeshes = [];
  cartonShells = [];
  skuColors = new Map();
}

function colorForSku(sku) {
  if (!skuColors.has(sku)) {
    skuColors.set(sku, PALETTE[skuColors.size % PALETTE.length]);
  }
  return skuColors.get(sku);
}

function colorForCoolant(sku) {
  const text = String(sku || '').toUpperCase();
  return text.includes('DRY') ? COOLANT_COLORS.dry_ice : COOLANT_COLORS.blue_ice;
}

function buildScene(order) {
  clearScene();

  const totalItems = order.cartons.reduce((sum, c) => sum + c.placements.length, 0);
  const withEdges = totalItems <= 1500;
  const gap = 180;

  let cursor = 0;
  let maxLayer = 0;

  order.cartons.forEach((cartonPlan, cartonIndex) => {
    const c = cartonPlan.carton;
    const group = new THREE.Group();
    group.position.x = cursor;
    root.add(group);

    // 箱体轮廓
    const boxGeo = new THREE.BoxGeometry(c.length, c.height, c.width);
    const outline = new THREE.LineSegments(
      new THREE.EdgesGeometry(boxGeo),
      new THREE.LineBasicMaterial({ color: 0x6ea8fe, transparent: true, opacity: 0.85 }),
    );
    outline.position.set(c.length / 2, c.height / 2, c.width / 2);
    group.add(outline);

    const shell = new THREE.Mesh(
      new THREE.BoxGeometry(c.length, c.height, c.width),
      new THREE.MeshBasicMaterial({
        color: 0x6ea8fe,
        transparent: true,
        opacity: 0.035,
        depthWrite: false,
        side: THREE.DoubleSide,
      }),
    );
    shell.position.copy(outline.position);
    group.add(shell);
    cartonShells.push(shell);

    // 物品
    for (const p of cartonPlan.placements) {
      maxLayer = Math.max(maxLayer, p.layer);
      const color = p.is_coolant ? colorForCoolant(p.sku) : colorForSku(p.sku);

      let geometry;
      let px, py, pz;

      if (p.kind === 'bottle') {
        geometry = new THREE.CylinderGeometry(p.d / 2, p.d / 2, p.h, 22);
        px = p.cx;
        py = p.z + p.h / 2;
        pz = p.cy;
      } else {
        geometry = new THREE.BoxGeometry(p.w, p.h, p.l);
        px = p.x + p.w / 2;
        py = p.z + p.h / 2;
        pz = p.y + p.l / 2;
      }

      const material = new THREE.MeshStandardMaterial({
        color,
        roughness: p.is_coolant ? 0.25 : 0.55,
        metalness: p.is_coolant ? 0.15 : 0.04,
        transparent: Boolean(p.is_coolant),
        opacity: p.is_coolant ? 0.9 : 1,
      });

      const mesh = new THREE.Mesh(geometry, material);
      mesh.position.set(px, py, pz);
      mesh.userData = {
        placement: p,
        cartonIndex,
        baseY: py,
      };
      group.add(mesh);
      itemMeshes.push(mesh);

      if (withEdges && p.kind === 'box') {
        const edges = new THREE.LineSegments(
          new THREE.EdgesGeometry(geometry),
          new THREE.LineBasicMaterial({ color: 0x0b1118, transparent: true, opacity: 0.35 }),
        );
        edges.position.copy(mesh.position);
        edges.userData.follow = mesh;
        group.add(edges);
        mesh.userData.edges = edges;
      }
    }

    cursor += c.length + gap;
  });

  state.layerMax = maxLayer;
  state.layerShow = maxLayer;

  $('layerSlider').max = String(maxLayer);
  $('layerSlider').value = String(maxLayer);
  $('layerValue').textContent = '全部';

  applyView();
  fitCamera();
}

function applyView() {
  for (const mesh of itemMeshes) {
    const p = mesh.userData.placement;
    const visible = p.layer <= state.layerShow;
    mesh.visible = visible;
    mesh.position.y = mesh.userData.baseY + p.layer * state.explode;
    if (mesh.userData.edges) {
      mesh.userData.edges.visible = visible;
      mesh.userData.edges.position.y = mesh.position.y;
    }
  }
  for (const shell of cartonShells) {
    shell.visible = state.showCarton;
  }
  grid.visible = true;
}

function fitCamera() {
  const box = new THREE.Box3().setFromObject(root);
  if (box.isEmpty()) return;

  const center = box.getCenter(new THREE.Vector3());
  const min = box.min;
  const max = box.max;

  // 相机观察方向（略高于水平面）
  const dir = new THREE.Vector3(0.9, 0.66, 1.0).normalize();

  // 用临时对象建立相机坐标系，把 8 个角点投影进去求包络
  const probe = new THREE.Object3D();
  probe.position.copy(center).add(dir);
  probe.lookAt(center);
  probe.updateMatrixWorld(true);
  const inverse = new THREE.Matrix4().copy(probe.matrixWorld).invert();

  const corner = new THREE.Vector3();
  let halfX = 0;
  let halfY = 0;
  let halfDepth = 0;
  for (let i = 0; i < 8; i += 1) {
    corner.set(i & 1 ? max.x : min.x, i & 2 ? max.y : min.y, i & 4 ? max.z : min.z);
    corner.applyMatrix4(inverse);
    halfX = Math.max(halfX, Math.abs(corner.x));
    halfY = Math.max(halfY, Math.abs(corner.y));
    halfDepth = Math.max(halfDepth, Math.abs(corner.z));
  }

  const fovV = (camera.fov * Math.PI) / 180;
  const fovH = 2 * Math.atan(Math.tan(fovV / 2) * camera.aspect);

  const distanceV = halfY / Math.tan(fovV / 2) + halfDepth;
  const distanceH = halfX / Math.tan(fovH / 2) + halfDepth;
  const distance = Math.max(distanceV, distanceH) * 1.1;

  camera.position.copy(center).addScaledVector(dir, distance);
  camera.near = Math.max(distance / 500, 0.05);
  camera.far = distance * 40;
  camera.updateProjectionMatrix();

  controls.target.copy(center);
  controls.minDistance = distance * 0.08;
  controls.maxDistance = distance * 6;
  controls.update();

  grid.position.y = min.y - 0.5;
}

/* ------------------------------------------------------------------ *
 * 交互：悬浮提示
 * ------------------------------------------------------------------ */

const tooltip = $('tooltip');
const canvas = renderer.domElement;

canvas.addEventListener('pointermove', (event) => {
  const rect = canvas.getBoundingClientRect();
  pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
  pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;

  raycaster.setFromCamera(pointer, camera);
  const hits = raycaster.intersectObjects(itemMeshes.filter((m) => m.visible), false);

  if (!hits.length) {
    tooltip.classList.add('hidden');
    return;
  }

  const mesh = hits[0].object;
  const p = mesh.userData.placement;
  tooltip.innerHTML =
    `<b>${p.name || p.sku}</b><br>` +
    `${p.sku} · 第 ${p.layer + 1} 层<br>` +
    `箱 #${mesh.userData.cartonIndex + 1} · 坐标 (${p.x}, ${p.y}, ${p.z})`;
  tooltip.style.left = `${event.clientX}px`;
  tooltip.style.top = `${event.clientY}px`;
  tooltip.classList.remove('hidden');
});

canvas.addEventListener('pointerleave', () => tooltip.classList.add('hidden'));

/* ------------------------------------------------------------------ *
 * UI：订单列表
 * ------------------------------------------------------------------ */

function pct(value) {
  return `${(value * 100).toFixed(1)}%`;
}

function kg(grams) {
  if (grams >= 1000) return `${(grams / 1000).toFixed(2)} kg`;
  return `${Math.round(grams)} g`;
}

function renderOrderList() {
  const list = $('orderList');
  list.innerHTML = '';

  if (!state.data || !state.data.orders.length) {
    list.innerHTML = '<p class="empty-hint">导入数据后，这里会显示合并后的订单列表。</p>';
    return;
  }

  state.data.orders.forEach((order, index) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'order-item' + (index === state.selected ? ' active' : '');
    const warn = order.unplaced.length ? ` · 缺 ${order.unplaced.reduce((s, u) => s + u.qty, 0)} 件` : '';
    button.innerHTML = `
      <div class="row1">
        <span class="no">${order.order_no}</span>
        <span class="cartons">${order.carton_count} 箱</span>
      </div>
      <div class="meta">${order.item_count} 件 · 填充 ${pct(order.fill_rate)}${order.order_type ? ` · ${order.order_type}` : ''}${warn}</div>
      <div class="bar"><i style="width:${Math.min(order.fill_rate * 100, 100).toFixed(1)}%"></i></div>
    `;
    button.addEventListener('click', () => selectOrder(index));
    list.appendChild(button);
  });
}

function renderWarnings() {
  const box = $('warnings');
  box.innerHTML = '';
  const warnings = (state.data && state.data.warnings) || [];
  warnings.slice(0, 6).forEach((text) => {
    const div = document.createElement('div');
    div.className = 'warning-item';
    div.textContent = text;
    box.appendChild(div);
  });
}

function candidateList(order) {
  if (!order) return [];
  if (order.candidates && order.candidates.length) return order.candidates;
  return [order];
}

function selectOrder(index) {
  if (!state.data || !state.data.orders[index]) return;
  state.selected = index;
  state.candidate = 0;

  const order = state.data.orders[index];

  renderOrderList();

  $('emptyState').classList.add('hidden');
  $('orderTitle').textContent = order.order_no;
  $('excelBtn').disabled = false;
  $('exportBtn').disabled = false;
  $('resetViewBtn').disabled = false;

  renderCandidateTabs(order);
  selectCandidate(0);
}

function selectCandidate(index) {
  if (!state.data || state.selected < 0) return;
  const order = state.data.orders[state.selected];
  const cands = candidateList(order);
  const cand = cands[index];
  if (!cand) return;

  state.candidate = index;
  buildScene(cand);
  renderStats(cand);
  renderLegend(cand);
  renderCandidateTabs(order);

  const parts = [cand.label || '', cand.strategy || ''].filter(Boolean);
  $('orderSubtitle').textContent =
    `${parts.join(' · ')} · ${cand.carton_count} 箱 · ${cand.item_count} 件`;
}

function renderCandidateTabs(order) {
  const bar = $('candidateBar');
  bar.innerHTML = '';
  const cands = order.candidates || [];
  if (cands.length < 2) return;

  cands.forEach((cand, i) => {
    const button = document.createElement('button');
    button.type = 'button';
    const bad = !(cand.weight_ok && cand.coolant_ok && !(cand.unplaced && cand.unplaced.length));
    button.className = 'cand-btn' + (i === state.candidate ? ' active' : '') + (bad ? ' bad' : '');
    const codes = [...new Set(cand.cartons.map((c) => c.carton.code))].join('+') || '-';
    const flag = !cand.weight_ok
      ? '承重超标'
      : !cand.coolant_ok
        ? '冷媒未放全'
        : (cand.unplaced && cand.unplaced.length)
          ? `剩 ${cand.unplaced.reduce((n, u) => n + u.qty, 0)} 件`
          : '';
    button.innerHTML = `
      <span class="cand-name">${cand.label || `方案${i + 1}`} · ${codes} × ${cand.carton_count}箱</span>
      <span class="cand-meta">${cand.item_count} 件 · 填充 ${pct(cand.fill_rate)} · ${kg(cand.total_weight)}</span>
      ${flag ? `<span class="cand-flag">${flag}</span>` : ''}
    `;
    button.addEventListener('click', () => selectCandidate(i));
    bar.appendChild(button);
  });
}

function renderStats(cand) {
  const box = $('stats');
  if (!cand || !cand.cartons || !cand.cartons.length) {
    box.innerHTML = '<div class="stat-row"><span>结果</span><b>无可用方案</b></div>';
    return;
  }

  const types = [...new Set(cand.cartons.map((c) => c.carton.name || c.carton.code))];
  const maxHeight = Math.max(0, ...cand.cartons.map((c) => c.used_height));
  const temp = cand.temperature || {};
  const coolant = (cand.coolant_usage || [])
    .map((u) => `${u.kind_label || u.code}×${u.qty}`)
    .join('、') || '无';

  const rows = [
    ['订单类型', cand.order_type || '—'],
    ['温度', temp.min !== undefined ? `${temp.min}~${temp.max}℃` : '—'],
    ['箱型', types.length === 1 ? types[0] : `${types.length} 种`],
    ['箱数', `${cand.carton_count}`],
    ['货品件数', `${cand.item_count}`],
    ['冷媒', coolant],
    ['填充率', pct(cand.fill_rate)],
    ['总重(含冷媒)', kg(cand.total_weight)],
    ['承重', cand.weight_ok ? '达标' : '超重'],
    ['最高堆高', `${Math.round(maxHeight)} mm`],
  ];

  box.innerHTML = rows
    .map(([label, value]) => `<div class="stat-row"><span>${label}</span><b>${value}</b></div>`)
    .join('');

  if (cand.unplaced && cand.unplaced.length) {
    const count = cand.unplaced.reduce((s, u) => s + u.qty, 0);
    box.insertAdjacentHTML(
      'beforeend',
      `<div class="stat-row"><span>未装箱</span><b style="color:#ffb020">${count} 件</b></div>`,
    );
  }
}

function renderLegend(cand) {
  const box = $('legend');
  box.innerHTML = '';
  (cand.items || []).forEach((item) => {
    const div = document.createElement('div');
    div.className = 'legend-item';
    div.innerHTML =
      `<span class="legend-dot" style="background:${colorForSku(item.sku)}"></span>` +
      `<span>${item.name || item.sku} × ${item.qty}</span>`;
    box.appendChild(div);
  });
  (cand.coolant_usage || []).forEach((u) => {
    if (!u.qty) return;
    const div = document.createElement('div');
    div.className = 'legend-item';
    div.innerHTML =
      `<span class="legend-dot" style="background:${colorForCoolant(u.code)}"></span>` +
      `<span>${u.kind_label || u.code} × ${u.qty}（冷媒）</span>`;
    box.appendChild(div);
  });
}

/* ------------------------------------------------------------------ *
 * API
 * ------------------------------------------------------------------ */

function showToast(message, isError = false) {
  const toast = $('toast');
  toast.textContent = message;
  toast.classList.toggle('error', isError);
  toast.classList.remove('hidden');
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => toast.classList.add('hidden'), 3600);
}

function setLoading(on) {
  $('loading').classList.toggle('hidden', !on);
}

function coolantSpec(prefix) {
  return {
    enabled: $(`${prefix}Enabled`).checked,
    ratio: (Number($(`${prefix}Ratio`).value) || 0) / 100,
    length: Number($(`${prefix}Length`).value) || 0,
    width: Number($(`${prefix}Width`).value) || 0,
    height: Number($(`${prefix}Height`).value) || 0,
    weight: Number($(`${prefix}Weight`).value) || 0,
  };
}

function loadPresetIntoForm(name) {
  const preset = state.presets[name];
  if (!preset) return;
  $('tempMin').value = preset.temp_min;
  $('tempMax').value = preset.temp_max;
  $('goodsRatio').value = Math.round(preset.goods_ratio * 100);
  $('blueRatio').value = Math.round(preset.blue_ice * 100);
  $('dryRatio').value = Math.round(preset.dry_ice * 100);
  $('blueEnabled').checked = preset.blue_ice > 0;
  $('dryEnabled').checked = preset.dry_ice > 0;
  $('coldEnabled').checked = preset.enabled;
}

function saveFormToPreset() {
  const name = $('orderType').value;
  if (name === 'custom' || !state.presets[name]) return;
  state.presets[name] = {
    temp_min: Number($('tempMin').value),
    temp_max: Number($('tempMax').value),
    goods_ratio: (Number($('goodsRatio').value) || 0) / 100,
    blue_ice: $('blueEnabled').checked ? (Number($('blueRatio').value) || 0) / 100 : 0,
    dry_ice: $('dryEnabled').checked ? (Number($('dryRatio').value) || 0) / 100 : 0,
    enabled: $('coldEnabled').checked,
  };
}

function readConfig() {
  const patternButton = document.querySelector('#patternGroup button.active');
  saveFormToPreset();
  return {
    pattern: patternButton ? patternButton.dataset.value : 'hex',
    gap: Number($('gapInput').value) || 0,
    rotate_box: $('rotateBox').checked,
    respect_weight: $('respectWeight').checked,
    objective: $('objective').value,
    top_k: Number($('topK').value) || 3,
    weight_filter: $('weightFilter').checked,
    cold_chain: {
      enabled: $('coldEnabled').checked,
      order_type: $('orderType').value,
      temp_min: Number($('tempMin').value),
      temp_max: Number($('tempMax').value),
      ratio_basis: $('ratioBasis').value,
      goods_ratio: (Number($('goodsRatio').value) || 0) / 100,
      coolants: [
        {
          code: 'BLUE-ICE',
          name: '蓝冰',
          kind: 'blue_ice',
          temp_min: 0,
          temp_max: 10,
          ...coolantSpec('blue'),
        },
        {
          code: 'DRY-ICE',
          name: '干冰',
          kind: 'dry_ice',
          temp_min: -100,
          temp_max: -20,
          ...coolantSpec('dry'),
        },
      ],
      presets: state.presets,
    },
  };
}

async function parseError(response) {
  try {
    const data = await response.json();
    return data.detail || JSON.stringify(data);
  } catch {
    return `请求失败（HTTP ${response.status}）`;
  }
}

function applyResult(data) {
  state.data = data;
  state.selected = -1;

  $('orderTitle').textContent = '尚未选择订单';
  $('orderSubtitle').textContent = `${data.orders.length} 个订单 · 共 ${data.summary.carton_count} 箱`;
  $('exportBtn').disabled = true;
  $('excelBtn').disabled = true;
  $('resetViewBtn').disabled = true;
  $('stats').innerHTML = '';
  $('legend').innerHTML = '';
  $('candidateBar').innerHTML = '';
  $('emptyState').classList.remove('hidden');
  clearScene();

  renderOrderList();
  renderWarnings();

  if (data.orders.length) selectOrder(0);
}

async function runEstimate() {
  const boxes = $('boxesFile').files[0];
  const orders = $('ordersFile').files[0];
  if (!boxes || !orders) {
    showToast('请先选择「包装箱列表」和「订单明细」两个 Excel 文件', true);
    return;
  }

  state.files = { boxes, orders };

  const form = new FormData();
  form.append('boxes', boxes);
  form.append('orders', orders);
  form.append('config', JSON.stringify(readConfig()));
  form.append('include_placements', 'true');

  setLoading(true);
  try {
    const response = await fetch('/api/estimate', { method: 'POST', body: form });
    if (!response.ok) throw new Error(await parseError(response));
    applyResult(await response.json());
    showToast('预估完成');
  } catch (error) {
    showToast(String(error.message || error), true);
  } finally {
    setLoading(false);
  }
}

async function loadSample() {
  state.files = { boxes: null, orders: null };
  setLoading(true);
  try {
    const response = await fetch('/api/sample');
    if (!response.ok) throw new Error(await parseError(response));
    applyResult(await response.json());
    showToast('已加载示例数据');
  } catch (error) {
    showToast(String(error.message || error), true);
  } finally {
    setLoading(false);
  }
}

/* ------------------------------------------------------------------ *
 * 事件绑定
 * ------------------------------------------------------------------ */

function bindFileInput(inputId, nameId, dropId) {
  const input = $(inputId);
  const nameEl = $(nameId);
  const drop = $(dropId);

  input.addEventListener('change', () => {
    const file = input.files[0];
    if (file) {
      nameEl.textContent = file.name;
      drop.classList.add('filled');
    } else {
      nameEl.textContent = '点击选择 Excel 文件';
      drop.classList.remove('filled');
    }
  });
}

bindFileInput('boxesFile', 'boxesName', 'boxesDrop');
bindFileInput('ordersFile', 'ordersName', 'ordersDrop');

$('runBtn').addEventListener('click', runEstimate);
$('sampleBtn').addEventListener('click', loadSample);

document.querySelectorAll('#patternGroup button').forEach((button) => {
  button.addEventListener('click', () => {
    document.querySelectorAll('#patternGroup button').forEach((b) => b.classList.remove('active'));
    button.classList.add('active');
  });
});

$('layerSlider').addEventListener('input', (event) => {
  state.layerShow = Number(event.target.value);
  const max = Number(event.target.max);
  $('layerValue').textContent = state.layerShow >= max ? '全部' : `第 ${state.layerShow + 1} 层`;
  applyView();
});

$('explodeSlider').addEventListener('input', (event) => {
  state.explode = Number(event.target.value);
  $('explodeValue').textContent = `${state.explode} mm`;
  applyView();
});

$('showCarton').addEventListener('change', (event) => {
  state.showCarton = event.target.checked;
  applyView();
});

$('autoRotate').addEventListener('change', (event) => {
  state.autoRotate = event.target.checked;
});

$('resetViewBtn').addEventListener('click', () => {
  if (state.selected >= 0) fitCamera();
});

async function exportExcel() {
  if (!state.data) return;

  setLoading(true);
  try {
    let response;
    if (state.files.boxes && state.files.orders) {
      const form = new FormData();
      form.append('boxes', state.files.boxes);
      form.append('orders', state.files.orders);
      form.append('config', JSON.stringify(readConfig()));
      response = await fetch('/api/export', { method: 'POST', body: form });
    } else {
      response = await fetch('/api/sample/export');
    }
    if (!response.ok) throw new Error(await parseError(response));

    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = '订单装箱推荐方案.xlsx';
    a.click();
    URL.revokeObjectURL(url);
    showToast('已导出带「推荐装箱方案1/2/3」的 Excel');
  } catch (error) {
    showToast(String(error.message || error), true);
  } finally {
    setLoading(false);
  }
}

$('excelBtn').addEventListener('click', exportExcel);

$('orderType').addEventListener('change', () => {
  const name = $('orderType').value;
  if (name !== 'custom') loadPresetIntoForm(name);
});

['tempMin', 'tempMax', 'goodsRatio', 'blueRatio', 'dryRatio'].forEach((id) => {
  $(id).addEventListener('input', saveFormToPreset);
});

['blueEnabled', 'dryEnabled', 'coldEnabled'].forEach((id) => {
  $(id).addEventListener('change', () => {
    if (id === 'blueEnabled' && !$(id).checked) $('blueRatio').value = '0';
    if (id === 'dryEnabled' && !$(id).checked) $('dryRatio').value = '0';
    saveFormToPreset();
  });
});

loadPresetIntoForm('冷藏');

$('exportBtn').addEventListener('click', () => {
  if (!state.data || state.selected < 0) return;
  const order = state.data.orders[state.selected];
  const payload = {
    config: state.data.config,
    cartons: state.data.cartons,
    order,
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${order.order_no}-装箱方案.json`;
  a.click();
  URL.revokeObjectURL(url);
});

/* 开发调试钩子：控制台可通过 window.__VIEWER__ 检查场景状态 */
window.__VIEWER__ = { scene, camera, controls, root, state, fitCamera, buildScene };
