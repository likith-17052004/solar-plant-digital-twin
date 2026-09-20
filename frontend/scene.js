import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { Sky } from 'three/addons/objects/Sky.js';

// Metres throughout. Each rendered table contains the configured number of
// modules: 29 × 1.303 m wide, 2.384 m deep, for the reference Trina module.
// Civil works and terrain are illustrative, not a survey of Pavagada.
let seed = 731;
function random() { seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0; return seed / 4294967296; }
const clamp = THREE.MathUtils.clamp;

function canvasTexture(width, height, paint) {
  const c = document.createElement('canvas'); c.width = width; c.height = height;
  paint(c.getContext('2d'), width, height);
  const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace;
  t.wrapS = t.wrapT = THREE.RepeatWrapping; return t;
}

function moduleTexture() {
  return canvasTexture(384, 704, (c, w, h) => {
    c.fillStyle = '#86958e'; c.fillRect(0, 0, w, h);
    c.fillStyle = '#252f36'; c.fillRect(4, 4, w - 8, h - 8);
    const cw = (w - 16) / 6, ch = (h - 16) / 22;
    for (let row = 0; row < 22; row++) for (let col = 0; col < 6; col++) {
      const x = 8 + col * cw, y = 8 + row * ch;
      const b = Math.floor(random() * 8);
      c.fillStyle = `rgb(${17+b},${29+b},${40+b})`;
      c.beginPath(); c.roundRect(x + 1, y + 1, cw - 2, ch - 2, 2); c.fill();
      c.strokeStyle = 'rgba(138,159,164,.29)'; c.lineWidth = .6;
      for (let finger = 3; finger < ch; finger += 3) { c.beginPath(); c.moveTo(x + 2, y + finger); c.lineTo(x + cw - 2, y + finger); c.stroke(); }
      c.strokeStyle = 'rgba(189,199,195,.55)'; c.lineWidth = .85;
      for (let bar = 1; bar < 6; bar++) { c.beginPath(); c.moveTo(x + bar * cw / 6, y); c.lineTo(x + bar * cw / 6, y + ch); c.stroke(); }
    }
    const g = c.createLinearGradient(0, 0, w, h); g.addColorStop(0, '#b9d9de12'); g.addColorStop(1, '#00000000'); c.fillStyle = g; c.fillRect(4, 4, w-8, h-8);
  });
}

function soilTexture() {
  return canvasTexture(1024, 1024, (c, w, h) => {
    c.fillStyle = '#90805e'; c.fillRect(0, 0, w, h);
    for (let i = 0; i < 13000; i++) {
      const x = random()*w, y = random()*h, r = 1 + random()*10;
      c.fillStyle = ['#b0a17d26','#534d3122','#cec09c30','#65663d25'][i%4];
      c.beginPath(); c.ellipse(x,y,r,r*.4,random()*6,0,Math.PI*2); c.fill();
    }
    for(let i=0;i<1100;i++) { c.strokeStyle='#65643944'; const x=random()*w,y=random()*h; c.beginPath();c.moveTo(x,y);c.lineTo(x+random()*5-2,y-2-random()*5);c.stroke(); }
  });
}

function labelTexture(text, subtitle = '') {
  return canvasTexture(512, 160, (c,w,h) => {
    c.fillStyle='#e3e3d2';c.fillRect(0,0,w,h);c.fillStyle='#29413a';c.fillRect(0,0,14,h);
    c.font='bold 45px sans-serif';c.fillText(text,35,70);c.font='22px sans-serif';c.fillText(subtitle,35,118);
  });
}

// Volumetric clouds: a surrounding proxy sphere whose fragment shader
// raymarches genuine 3D density volumes (impostor-geometry raymarching - the
// sphere surrounds the camera and the cloud volume is defined by the layer
// altitude uniforms and the baked textures, not by the mesh). Two altitude
// bands stand in for real cloud-height data (low cumulus-like, high
// cirrus-like).
//
// Each layer's density is baked ONCE into a Data3DTexture spanning a real
// 10 km x 10 km footprint and that layer's altitude band - one byte of
// density per voxel, sampled trilinearly. This replaced an earlier 2D mask
// extruded through an analytic height curve, which could only ever produce
// heightfield-like shapes: a genuine 3D field is what lets a cloud bulge,
// overhang, and erode differently at different heights.
//
// Placement uses Worley cells (Worley 1996 - distinct rounded cells rather
// than a continuous haze), shaped vertically by a cumulus-like profile and
// eroded by 3D value-noise fbm. Everything is periodic over the tile, so the
// field repeats seamlessly instead of seaming at the domain edge.
//
// This is the seam for plugging in real cloud shapes later: a real 3D cloud
// field (model cloud-water content, a radar reflectivity volume, ...) is
// already exactly this shape - fill the same voxel grid from it and nothing
// below changes. Coverage (how much of the baked field is "on") stays a live
// per-snapshot uniform from cloud_cover_fraction, so the same baked shapes
// respond to real hourly weather.
//
// The scattering model (dual-lobe Henyey-Greenstein phase function, Beer's
// law light march, per-channel extinction, interleaved-gradient ray jitter)
// follows the standard real-time approach; Leonardo Awen Goncalves' MIT
// three.js implementation was a useful reference for how the pieces fit
// together: https://github.com/leoawen/volumetric-clouds
// Lowest the eye is allowed to go, so orbiting up never dips underground.
const MIN_EYE_HEIGHT = 2.2;

// Render layers, so the god-ray occlusion mask can draw the sun, the solid
// occluders and the clouds separately. LAYER_MASK_SUN is deliberately left
// off the camera: it holds a big flat emitter used only by the mask pass.
const LAYER_SOLID = 0, LAYER_MASK_SUN = 1, LAYER_CLOUD = 2, LAYER_SKY = 3;

// The sun's disc. Two things stop a bright circle from reading as a sticker:
// a genuinely hard limb, and limb darkening.
//
// Limb darkening is real and measurable - the photosphere is optically deep,
// so a sightline near the rim grazes through cooler, higher gas and comes back
// dimmer. The Eddington approximation gives I(mu)/I(0) = 1 - u(1 - mu), where
// mu is the cosine of the emission angle, equal to sqrt(1 - r^2) at fractional
// disc radius r. u is about 0.5 in the middle of the visible band, and larger
// toward the blue, which is why the rim is also slightly warmer than the core.
function sunDiscTexture() {
  const LIMB_DARKENING = 0.5;
  const EDGE = 0.97;          // the last 3% of the radius is anti-aliasing only
  const STOPS = 28;
  return canvasTexture(256, 256, (c, w, h) => {
    const g = c.createRadialGradient(w/2, h/2, 0, w/2, h/2, w/2);
    // Alpha stays at 1 across the whole disc and the limb profile lives in the
    // colour channels. With normal blending alpha means coverage, not
    // brightness - putting the darkening there would make the rim see-through
    // instead of dimmer, and the sky would show through the sun.
    for (let i = 0; i <= STOPS; i++) {
      const t = i / STOPS, r = t * EDGE;
      const mu = Math.sqrt(Math.max(0, 1 - r * r));
      const brightness = 1 - LIMB_DARKENING * (1 - mu);
      const red = Math.round(255 * brightness);
      const green = Math.round((253 - 14 * (1 - mu)) * brightness);
      const blue = Math.round((247 - 52 * (1 - mu)) * brightness);
      g.addColorStop(r, `rgba(${red},${green},${blue},1)`);
    }
    g.addColorStop(1, 'rgba(255,214,150,0)');
    c.fillStyle = g; c.fillRect(0, 0, w, h);
  });
}

// The glare around the disc. A true-size sun is about eleven pixels tall on a
// 1080p viewport - what lets you pick it out of a photograph is not its size
// but the aureole, the forward-scattered halo aerosols throw around it.
//
// That aureole falls off as roughly a power law in angle from the sun, not as
// a linear ramp: very steep within the first degree, with a tail that is still
// faintly there ten degrees out. Building the gradient from that law rather
// than from hand-picked stops is what makes it read as scattered light instead
// of an airbrushed circle.
function sunGlowTexture() {
  const HALF_ANGLE_DEG = 9.0;   // angle subtended at the sprite's own edge
  // Anchored at the disc's own radius, so the power law starts falling exactly
  // where the disc stops hiding it. The disc is opaque now, so anything the
  // glare puts inside that radius is wasted.
  const ANCHOR_DEG = 0.62;
  const FALLOFF = 1.7;
  const STOPS = 40;
  return canvasTexture(256, 256, (c, w, h) => {
    const g = c.createRadialGradient(w/2, h/2, 0, w/2, h/2, w/2);
    for (let i = 0; i <= STOPS; i++) {
      const t = i / STOPS;
      const theta = Math.max(ANCHOR_DEG, t * HALF_ANGLE_DEG);
      // The taper forces the profile to zero at the sprite's rim, so the quad's
      // own edge never shows as a ring.
      const alpha = Math.pow(ANCHOR_DEG / theta, FALLOFF) * (1 - t * t);
      // Whitish close in where scattering is near-neutral, warming outward.
      const green = Math.round(246 - 30 * t);
      const blue = Math.round(226 - 80 * t);
      g.addColorStop(t, `rgba(255,${green},${blue},${alpha.toFixed(4)})`);
    }
    c.fillStyle = g; c.fillRect(0, 0, w, h);
  });
}

// Kasten & Young (1989) relative optical air mass. Plain 1/sin(h) diverges at
// the horizon; this stays finite and is accurate to about 38 air masses there.
function relativeAirMass(elevationDeg) {
  if (elevationDeg < -1) return 40;
  const h = Math.max(elevationDeg, -1);
  return Math.min(40, 1 / (Math.sin(h * Math.PI / 180)
    + 0.50572 * Math.pow(h + 6.07995, -1.6364)));
}

// What the atmosphere does to the sun's own colour on the way down. Rayleigh
// scattering removes blue far faster than red (the lambda^-4 dependence that
// makes the sky blue in the first place), so the disc reddens as it sets. The
// optical depths below are representative sea-level values including aerosol.
//
// The hue shift is applied in full. The *dimming* is not: a real sun at the
// horizon is some four orders of magnitude fainter than at noon, which at a
// fixed rendering exposure would simply delete it, where a real eye would have
// adapted. So the overall level is compressed by a power while the ratios
// between channels are left exactly as the physics gives them.
const SUN_OPTICAL_DEPTH = [0.09, 0.14, 0.24];
const SUN_DIMMING_COMPRESSION = 0.62;
// Radiance of the disc at zero air mass, in the renderer's linear units. Set
// so the disc clips to white at noon; extinction then pulls it down until, low
// in the sky, it falls far enough out of the tone curve's compressive shoulder
// to read as genuinely orange rather than pale cream.
//
// It is a balance, and the measured trade-off is worth recording. At a sun
// elevation of 2.7 degrees, a value of 9 leaves the disc 99 levels warmer than
// neutral but two levels *darker* than the sky beside it - a dark orange hole
// rather than a sun, because the atmospheric model's own sky clips to white
// that close to the horizon. Doubling it puts the disc above the sky but
// bleaches the warmth to 58. This sits between: warmth about 65, and the disc
// is the brightest thing in frame at every elevation.
const SUN_PEAK_RADIANCE = 26.0;

function sunExtinction(elevationDeg) {
  const airMass = relativeAirMass(elevationDeg);
  const transmitted = SUN_OPTICAL_DEPTH.map(depth => Math.exp(-depth * airMass));
  const peak = Math.max(...transmitted);
  const dim = Math.pow(peak, SUN_DIMMING_COMPRESSION);
  return transmitted.map(value => (value / peak) * dim);
}

// Radial blur from the sun's screen position - "Volumetric Light Scattering
// as a Post-Process" (Mitchell, GPU Gems 3). Whatever is black in the mask
// casts no light, so clouds drawn into it punch holes in the rays.
const GODRAY_FRAGMENT_SHADER = `
  uniform sampler2D tMask;
  uniform vec2 uSunPos;
  uniform float uDensity, uWeight, uDecay, uExposure;
  uniform float uSunRadius, uAspect;
  varying vec2 vUv;
  void main() {
    const int SAMPLES = 48;
    vec2 uv = vUv;
    vec2 delta = (uv - uSunPos) * (uDensity / float(SAMPLES));
    float decay = 1.0;
    vec3 color = texture2D(tMask, uv).rgb;
    for (int i = 0; i < SAMPLES; i++) {
      uv -= delta;
      color += texture2D(tMask, uv).rgb * decay * uWeight;
      decay *= uDecay;
    }
    // The radial blur peaks exactly on the sun, because every sample marches
    // toward it. That peak is an artifact: the photosphere is opaque, so there
    // is no scattered light reaching the eye from behind the disc, and adding
    // it there only clipped the disc to white - which is why the setting sun
    // rendered pure white however red its own colour was. Suppress the term
    // inside the disc's own solid angle and feather out over roughly its
    // diameter; the disc itself covers the hole this leaves.
    // Suppress fully across the whole disc and feather out over about its own
    // diameter beyond that. Starting the feather inside the disc leaves the
    // rim brighter than the centre, which inverts the limb darkening and makes
    // the sun read as a ring.
    float fromSun = length((vUv - uSunPos) * vec2(uAspect, 1.0));
    float outsideDisc = smoothstep(uSunRadius, uSunRadius * 3.0, fromSun);
    gl_FragColor = vec4(color * uExposure * outsideDisc, 1.0);
  }
`;
const FULLSCREEN_VERTEX_SHADER = `
  varying vec2 vUv;
  void main() { vUv = uv; gl_Position = vec4(position.xy, 0.0, 1.0); }
`;

const CLOUD_DOMAIN_M = 10000;

// How far from the eye the sun sprites are parked, and the disc's size there.
// At this distance scale is effectively angular size (radians) times distance.
const SUN_DISTANCE_M = 9000;
const SUN_DISC_SCALE = 195;   // 1.24 degrees, against the real sun's 0.53. A
                              // deliberate exaggeration: at true size the disc
                              // is about 11 px on a 1080p viewport, too small
                              // to read as anything but a dot. The limb
                              // darkening and the extinction colour both need
                              // some pixels to land on to be seen at all.

// Tone-mapping exposure, deliberately low. At 0.88 the sky one degree from the
// sun rendered at 245/255 while the disc clipped at 255 - four percent
// contrast, so the sun was invisible no matter how bright the sprite was made,
// and no additive glare could lift a value already at the top of the curve.
// Dropping exposure is the only thing that buys that headroom back. It also
// restores the sky's colour: measured blue-minus-red went from 16 to 29, and
// disc-to-sky contrast from 18 to 48. Every scene light is scaled by
// LIGHT_GAIN so the ground does not darken with it - measured soil went
// 140/136/96 to 132/122/78, slightly richer rather than dimmer.
const EXPOSURE = 0.45;
const LIGHT_GAIN = 2.3;

function hash3i(x, y, z, seed) {
  let h = Math.imul(x | 0, 374761393) ^ Math.imul(y | 0, 668265263) ^ Math.imul(z | 0, 1442695041) ^ Math.imul(seed | 0, 2246822519);
  h = Math.imul(h ^ (h >>> 13), 1274126177);
  return ((h ^ (h >>> 16)) >>> 0) / 4294967296;
}
const wrapIndex = (v, period) => ((v % period) + period) % period;
const smootherstep = t => t * t * (3 - 2 * t);
const between = (edge0, edge1, x) => smootherstep(clamp((x - edge0) / (edge1 - edge0), 0, 1));

// Worley distance to the nearest jittered feature point, on a grid that wraps
// every `cells` so the tile is seamless. It also reports which cell won, so
// each convective cell can carry its own properties - how tall it grows, which
// way it leans - hashed from that cell id.
function worleyCellPeriodic(x, y, cells, seed) {
  const cx = Math.floor(x), cy = Math.floor(y), fx = x - cx, fy = y - cy;
  let nearest = 4, ownerX = 0, ownerY = 0;
  for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
    const gx = wrapIndex(cx + dx, cells), gy = wrapIndex(cy + dy, cells);
    const px = dx + hash3i(gx, gy, 0, seed) - fx, py = dy + hash3i(gx, gy, 1, seed) - fy;
    const d = px * px + py * py;
    if (d < nearest) { nearest = d; ownerX = gx; ownerY = gy; }
  }
  return { distance: Math.sqrt(nearest), ownerX, ownerY };
}

// 3D Worley. Inverted, this is "billow" noise - rounded lumps packed together,
// rather than the smooth rolling hills of value noise. It is what gives a
// cumulus its cauliflower surface, and value noise cannot imitate it.
function worley3Periodic(x, y, z, cells, seed) {
  const cx = Math.floor(x), cy = Math.floor(y), cz = Math.floor(z);
  const fx = x - cx, fy = y - cy, fz = z - cz;
  let nearest = 9;
  for (let dz = -1; dz <= 1; dz++) for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
    const gx = wrapIndex(cx + dx, cells), gy = wrapIndex(cy + dy, cells), gz = wrapIndex(cz + dz, cells);
    const px = dx + hash3i(gx, gy, gz, seed) - fx;
    const py = dy + hash3i(gx, gy, gz, seed + 7919) - fy;
    const pz = dz + hash3i(gx, gy, gz, seed + 15486) - fz;
    const d = px * px + py * py + pz * pz;
    if (d < nearest) nearest = d;
  }
  return Math.sqrt(nearest);
}

// 81 hashes per sample is far too slow to evaluate per voxel across a
// million-voxel volume, so the billow field is baked once into a small
// periodic tile and read back with trilinear interpolation.
function bakeBillowTile(size, cells, seed) {
  const tile = new Float32Array(size * size * size);
  for (let z = 0; z < size; z++) for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) {
    tile[x + size * (y + size * z)] = 1 - worley3Periodic(
      (x + 0.5) / size * cells, (y + 0.5) / size * cells, (z + 0.5) / size * cells, cells, seed);
  }
  return tile;
}
function sampleTile3(tile, size, x, y, z) {
  const x0 = Math.floor(x), y0 = Math.floor(y), z0 = Math.floor(z);
  const fx = x - x0, fy = y - y0, fz = z - z0;
  const X0 = wrapIndex(x0, size), X1 = wrapIndex(x0 + 1, size);
  const Y0 = wrapIndex(y0, size), Y1 = wrapIndex(y0 + 1, size);
  const Z0 = wrapIndex(z0, size), Z1 = wrapIndex(z0 + 1, size);
  const at = (a, b, c) => tile[a + size * (b + size * c)];
  const lerp = (a, b, t) => a + (b - a) * t;
  const face = zi => lerp(lerp(at(X0, Y0, zi), at(X1, Y0, zi), fx),
                          lerp(at(X0, Y1, zi), at(X1, Y1, zi), fx), fy);
  return lerp(face(Z0), face(Z1), fz);
}
function sampleGrid2(grid, sx, sz, x, z) {
  const x0 = Math.floor(x), z0 = Math.floor(z), fx = x - x0, fz = z - z0;
  const X0 = wrapIndex(x0, sx), X1 = wrapIndex(x0 + 1, sx);
  const Z0 = wrapIndex(z0, sz), Z1 = wrapIndex(z0 + 1, sz);
  const lerp = (a, b, t) => a + (b - a) * t;
  return lerp(lerp(grid[X0 + sx * Z0], grid[X1 + sx * Z0], fx),
              lerp(grid[X0 + sx * Z1], grid[X1 + sx * Z1], fx), fz);
}

// How wide a cumulus is at a given fraction of its own height, as a share of
// its full width. This is the function that makes the cloud a solid rather
// than an extrusion: a flat condensation-level base, the widest section about
// a third of the way up, then a rounded cauliflower dome.
function cumulusWidth(heightFraction, baseSharpness, widestAt) {
  if (heightFraction < 0 || heightFraction > 1) return 0;
  const rise = between(0, baseSharpness, heightFraction);
  if (heightFraction <= widestAt) return rise;
  const u = (heightFraction - widestAt) / (1 - widestAt);
  return rise * Math.sqrt(Math.max(0, 1 - u * u));
}

// Bakes one layer's density volume. `sx`/`sz` span CLOUD_DOMAIN_M horizontally
// (repeating), `sy` spans that layer's altitude band. Stored values are the
// un-thresholded shape: the shader applies the live coverage threshold, so
// rising coverage both adds cloud cells and grows the surviving ones taller.
//
// The shape is genuinely three-dimensional. An earlier version multiplied a
// 2D footprint by a single height envelope shared by every column, which meant
// every cloud had the same cross-section at every altitude - an extruded
// silhouette that read as flat from any angle. Here the *width* varies with
// height (`cumulusWidth`), each convective cell gets its own tower height and
// lean, and the surface is eroded with 3D billow noise.
function bakeCloudVolume(sx, sy, sz, options) {
  const { cells, erosion, billowCells, baseSharpness, widestAt, lean, seed,
          minTower, maxTower } = options;
  const data = new Uint8Array(sx * sy * sz);
  // Per-column fields, all keyed off the same Worley cell so one tower keeps
  // one identity: how far it is from its cell centre, how high it climbs, and
  // which way the wind shears it.
  const placement = new Float32Array(sx * sz);
  const tower = new Float32Array(sx * sz);
  const leanX = new Float32Array(sx * sz), leanZ = new Float32Array(sx * sz);
  for (let k = 0; k < sz; k++) for (let i = 0; i < sx; i++) {
    const cell = worleyCellPeriodic((i + 0.5) / sx * cells, (k + 0.5) / sz * cells, cells, seed);
    const index = i + sx * k;
    // Worley on its own puts one cloud in every cell, on a grid the eye picks
    // out immediately from above. Giving each cell its own strength lets weak
    // ones fall under the coverage threshold entirely, so the field has real
    // gaps and clusters instead of a lattice.
    const strength = 0.4 + 0.6 * hash3i(cell.ownerX, cell.ownerY, 41, seed);
    placement[index] = clamp(1 - cell.distance, 0, 1) * strength;
    tower[index] = minTower + (maxTower - minTower) * hash3i(cell.ownerX, cell.ownerY, 17, seed);
    leanX[index] = (hash3i(cell.ownerX, cell.ownerY, 23, seed) - 0.5) * 2;
    leanZ[index] = (hash3i(cell.ownerX, cell.ownerY, 29, seed) - 0.5) * 2;
  }
  const billowSize = 48;
  const billow = bakeBillowTile(billowSize, 6, seed + 91);
  const billowScale = billowSize / sx * billowCells;

  for (let k = 0; k < sz; k++) for (let j = 0; j < sy; j++) {
    const h = (j + 0.5) / sy;
    for (let i = 0; i < sx; i++) {
      const index = i + sx * k;
      // Shear the lookup with height so towers lean instead of standing as
      // vertical extrusions, and so the silhouette changes as you orbit.
      const x = i + 0.5 + leanX[index] * h * lean;
      const z = k + 0.5 + leanZ[index] * h * lean;
      const towerHeight = sampleGrid2(tower, sx, sz, x, z);
      const width = cumulusWidth(h / towerHeight, baseSharpness, widestAt);
      if (width <= 0.001) { data[i + sx * (j + sy * k)] = 0; continue; }
      // Narrow sections keep only the core of the footprint; wide sections
      // keep almost all of it. Renormalising holds the peak at 1 so the
      // shader's coverage threshold still bites evenly at every altitude.
      const cut = 1 - width;
      const raw = sampleGrid2(placement, sx, sz, x, z);
      let value = (raw - cut) / Math.max(1e-3, 1 - cut);
      if (value > 0.004) {
        const lumps = sampleTile3(billow, billowSize,
          x * billowScale, (j + 0.5) * billowScale * (sx / sy) * 0.35, z * billowScale);
        value *= 1 - erosion * (1 - lumps);
      }
      data[i + sx * (j + sy * k)] = clamp(Math.round(value * 255), 0, 255);
    }
  }
  const texture = new THREE.Data3DTexture(data, sx, sy, sz);
  texture.format = THREE.RedFormat; texture.type = THREE.UnsignedByteType;
  texture.minFilter = texture.magFilter = THREE.LinearFilter;
  texture.wrapS = texture.wrapR = THREE.RepeatWrapping;
  texture.wrapT = THREE.ClampToEdgeWrapping;
  texture.unpackAlignment = 1; texture.needsUpdate = true;
  return texture;
}

const CLOUD_VERTEX_SHADER = `
  out vec3 vWorldPosition;
  void main() {
    vec4 worldPosition = modelMatrix * vec4(position, 1.0);
    vWorldPosition = worldPosition.xyz;
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  }
`;
const CLOUD_FRAGMENT_SHADER = `
  precision highp float;
  precision highp sampler3D;
  in vec3 vWorldPosition;
  // GLSL3 declares its own fragment output; three.js does not supply one for
  // a ShaderMaterial, so gl_FragColor does not exist here.
  out vec4 fragColor;
  uniform float uTime;
  uniform float uDriftSpeed;
  uniform float uCoverageLow;
  uniform float uCoverageHigh;
  uniform float uDensityScale;
  uniform float uOcclusionMode;
  uniform vec3 uSunDirection;
  uniform vec3 uSunColor;
  uniform float uSunIntensity;
  uniform vec3 uAmbientColor;
  uniform vec3 uGroundColor;
  uniform float uAmbientIntensity;
  uniform float uPowder;
  uniform int uSteps;
  uniform sampler3D uLowCloudVolume;
  uniform sampler3D uHighCloudVolume;
  uniform float uDomain;
  uniform float uLowBase, uLowTop;
  uniform float uHighBase, uHighTop;

  const float PI = 3.14159265359;
  // Neutral. Cloud droplets are large compared with visible wavelengths, so
  // Mie scattering attenuates all three channels almost equally - that is
  // precisely why clouds are white and not coloured. An earlier version
  // attenuated blue faster to keep thick cores "warm", which made the
  // transmitted light yellow; multiplied by blue skylight fill, that landed
  // on green, and every cloud in the scene was faintly olive.
  const vec3 EXTINCTION = vec3(1.0);

  // Henyey & Greenstein (1941), "Diffuse radiation in the Galaxy" - the
  // standard phase function for how much light a medium scatters towards a
  // given angle. g > 0 is forward scattering.
  float henyeyGreenstein(float g, float mu) {
    float gg = g * g;
    return (1.0 / (4.0 * PI)) * ((1.0 - gg) / pow(1.0 + gg - 2.0 * g * mu, 1.5));
  }
  // Cloud droplets scatter strongly forward and weakly backward; mixing two
  // lobes captures both. This is what gives clouds bright rims when you look
  // towards the sun - without it every cloud reads as flat grey no matter
  // where the sun is.
  float phaseFunction(float mu, float g) {
    return mix(henyeyGreenstein(-g, mu), henyeyGreenstein(g, mu), 0.8);
  }
  // Interleaved gradient noise (Jimenez 2014): distributes the ray-jitter
  // error far more evenly than a white-noise hash at the same cost.
  float interleavedGradientNoise(vec2 pixel) {
    return fract(52.9829189 * fract(dot(pixel, vec2(0.06711056, 0.00583715))));
  }
  // Fixed spread directions for the light march, so the shadow it produces is
  // a soft gradient rather than a hard edge traced along one line.
  const vec3 CONE[6] = vec3[6](
    vec3( 0.38, 0.15, -0.28), vec3(-0.31, 0.27,  0.36), vec3( 0.22,-0.34,  0.19),
    vec3(-0.40,-0.18, -0.24), vec3( 0.12, 0.42,  0.30), vec3(-0.16,-0.36,  0.41));

  float hash(vec3 p) {
    p = fract(p * 0.3183099 + 0.1);
    p *= 17.0;
    return fract(p.x * p.y * p.z * (p.x + p.y + p.z));
  }
  float valueNoise(vec3 x) {
    vec3 i = floor(x), f = fract(x);
    f = f * f * (3.0 - 2.0 * f);
    return mix(
      mix(mix(hash(i + vec3(0,0,0)), hash(i + vec3(1,0,0)), f.x),
          mix(hash(i + vec3(0,1,0)), hash(i + vec3(1,1,0)), f.x), f.y),
      mix(mix(hash(i + vec3(0,0,1)), hash(i + vec3(1,0,1)), f.x),
          mix(hash(i + vec3(0,1,1)), hash(i + vec3(1,1,1)), f.x), f.y),
      f.z);
  }

  float layerDensity(vec3 p, sampler3D volume, float coverage, float base, float top) {
    if (coverage <= 0.001) return 0.0;
    float h = (p.y - base) / (top - base);
    if (h < 0.0 || h > 1.0) return 0.0;
    // The whole field advects horizontally; the tile is periodic, so this
    // scrolls seamlessly rather than sliding a seam across the sky.
    vec3 drift = vec3(uTime * uDriftSpeed, 0.0, uTime * uDriftSpeed * 0.35);
    vec2 uv = (p.xz + drift.xz) / uDomain + 0.5;
    float shape = texture(volume, vec3(uv.x, h, uv.y)).r;
    // Coverage slides a fixed-width ramp across the baked density
    // distribution. Both ends matter: the ramp never reaches 1.0 (baked
    // values rarely do either, and a full-range smoothstep left every cloud a
    // barely-there haze), and the bottom end stops short of 0 so full
    // overcast still varies in thickness instead of saturating the whole
    // field to one flat, structureless grey. These numbers track the *baked*
    // distribution, which the 3D-shape rewrite changed: the volume is now
    // renormalised so each altitude peaks near 1, so the old thresholds cut
    // away almost everything and left a haze.
    float threshold = mix(0.50, 0.03, coverage);
    float d = smoothstep(threshold, threshold + 0.30, shape);
    if (d <= 0.0) return 0.0;
    // The baked volume is ~78 m per voxel: fine at distance, mush up close.
    // High-frequency noise erodes the silhouette back to something crisp,
    // weighted so it bites at thin edges and leaves dense cores alone.
    float detail = valueNoise((p + drift) * 0.01);
    d -= (1.0 - d) * detail * 0.65;
    return clamp(d, 0.0, 1.0);
  }
  float density(vec3 p) {
    float low = layerDensity(p, uLowCloudVolume, uCoverageLow, uLowBase, uLowTop);
    float high = layerDensity(p, uHighCloudVolume, uCoverageHigh, uHighBase, uHighTop) * 0.55;
    return max(low, high);
  }
  void main() {
    vec3 rayOrigin = cameraPosition;
    vec3 rayDir = normalize(vWorldPosition - cameraPosition);
    // Near-horizontal rays make t0/t1 blow up towards infinity, which then
    // feeds absurd world-space coordinates into the density lookups below
    // and shows up as a bright precision-loss seam right at the horizon.
    // Bail out well before that rather than only guarding division by ~zero.
    if (abs(rayDir.y) < 0.02) discard;
    float slabMin = uLowBase, slabMax = uHighTop;
    float t0 = (slabMin - rayOrigin.y) / rayDir.y;
    float t1 = (slabMax - rayOrigin.y) / rayDir.y;
    float tNear = max(min(t0, t1), 0.0);
    float tFar = min(max(t0, t1), tNear + 9000.0);
    if (tFar <= tNear || tNear > 20000.0 || (uCoverageLow <= 0.001 && uCoverageHigh <= 0.001)) discard;

    // Empty sky is most of the slab, so marching it at the same resolution as
    // the cloud interior wastes nearly every sample. The ray strides through
    // clear air and drops to short steps on contact, which buys roughly three
    // times the detail inside the cloud for the same budget - and detail
    // inside the cloud is exactly what a solid shape is made of.
    const int MAX_STEPS = 160;
    float coarse = (tFar - tNear) / float(uSteps);
    float fine = coarse * 0.32;
    // Starting every ray at the same offset makes the fixed step planes show
    // up as concentric rings through the cloud. Jittering the start per pixel
    // trades that banding for fine noise, which reads as cloud texture.
    float t = tNear + coarse * interleavedGradientNoise(gl_FragCoord.xy);
    float stepLength = coarse;
    int emptyRun = 0;
    // Scattering angle between the view ray and the sun, constant along the ray.
    float mu = dot(rayDir, uSunDirection);

    vec3 accum = vec3(0.0);
    vec3 transmit = vec3(1.0);
    for (int i = 0; i < MAX_STEPS; i++) {
      if (t > tFar) break;
      vec3 pos = rayOrigin + rayDir * t;
      float d = density(pos);
      if (d > 0.005) {
        if (stepLength > fine * 1.5) {
          // Crossed into cloud on a long stride: back up and re-enter at the
          // short step, so the surface is not quantised onto the coarse grid.
          t = max(tNear, t - stepLength);
          stepLength = fine;
          continue;
        }
        // Beer's law along the sun direction. The steps grow so that a few
        // samples still span a cloud-sized distance - fixed short steps
        // saturated almost everywhere and flattened every cloud to one grey.
        // Each sample is nudged off-axis by a widening cone, which softens the
        // self-shadow into a gradient instead of a hard band and is what makes
        // the rounded form legible rather than merely lit.
        float lightDepth = 0.0, lightStep = 50.0;
        vec3 lp = pos;
        for (int j = 0; j < 6; j++) {
          lp += uSunDirection * lightStep + CONE[j] * lightStep * 0.35;
          lightDepth += density(lp) * lightStep;
          lightStep *= 1.7;
        }
        float opticalLight = lightDepth * uDensityScale;

        // A single Beer's-law term treats every photon as having scattered
        // once and then reached the eye, which makes a thick cloud dark and
        // muddy. Real clouds are white *because* light bounces many times
        // inside them. Summing a few "scattering octaves" with successively
        // weaker extinction, weaker contribution and flatter phase
        // approximates those later bounces cheaply, with no extra density
        // sampling - Schneider & Vos, "The Real-time Volumetric Cloudscapes
        // of Horizon Zero Dawn" (SIGGRAPH 2015). Without it no amount of
        // geometry detail stops the cloud looking like wet cardboard.
        float energy = 0.0;
        float attenuation = 1.0, contribution = 1.0, eccentricity = 0.3;
        for (int o = 0; o < 3; o++) {
          // The powdered-sugar term darkens edges facing the sun, where light
          // escapes sideways before it can scatter back to the eye.
          float beer = exp(-opticalLight * attenuation);
          float powder = 1.0 - exp(-opticalLight * attenuation * 2.0);
          energy += contribution * mix(beer, beer * powder * 2.0, uPowder)
                  * phaseFunction(mu, eccentricity);
          attenuation *= 0.55; contribution *= 0.55; eccentricity *= 0.6;
        }

        // Sky light comes from above and bounced ground light from below, so
        // a cloud is cool and bright on top and warm and dark underneath.
        // Without this gradient every voxel gets identical fill and the
        // silhouette flattens out again however good the geometry is. The
        // ground term fades out fast: it tints the base, it does not own the
        // cloud.
        float heightFraction = clamp((pos.y - uLowBase) / (uLowTop - uLowBase), 0.0, 1.0);
        // Even directly underneath, most of the fill a cloud base receives is
        // still skylight scattered around it - the ground bounce only tints
        // it. Letting the soil colour own the base outright turned the lower
        // half of every cloud brown.
        // Only a modest share: skylight is blue and soil bounce is orange, and
        // mixing the two in equal measure lands squarely on a desaturated
        // green, which is what turned every cloud base olive.
        vec3 ambientTone = mix(mix(uAmbientColor, uGroundColor, 0.28), uAmbientColor,
                               smoothstep(0.0, 0.45, heightFraction));

        vec3 sunScatter = uSunColor * uSunIntensity * energy;
        vec3 ambientScatter = ambientTone * uAmbientIntensity * (0.62 + 0.38 * heightFraction);
        float opticalStep = d * stepLength * uDensityScale;
        // Front-to-back: add this step's scattered light attenuated by what
        // the ray has already passed through, then attenuate for the next.
        // The scattered fraction is the analytic integral over the step, not
        // the raw optical depth - at these step sizes that product exceeds 1
        // and compounds into a blown-out sky if used directly.
        float scattered = 1.0 - exp(-opticalStep);
        accum += transmit * (sunScatter + ambientScatter) * scattered;
        transmit *= exp(-opticalStep * EXTINCTION);
        if (max(transmit.r, max(transmit.g, transmit.b)) < 0.02) break;
        emptyRun = 0;
      } else if (stepLength < coarse) {
        // Left the cloud: stay fine for a few samples in case this was only a
        // gap, then stride again.
        emptyRun++;
        if (emptyRun > 8) { stepLength = coarse; emptyRun = 0; }
      }
      t += stepLength;
    }
    float alpha = 1.0 - dot(transmit, vec3(1.0 / 3.0));
    if (alpha < 0.01) discard;
    // In the god-ray mask the cloud contributes only its opacity, in black,
    // so dense cloud blocks the sun and the rays break up behind it.
    if (uOcclusionMode > 0.5) { fragColor = vec4(0.0, 0.0, 0.0, alpha); return; }
    fragColor = vec4(accum / max(alpha, 0.001), alpha);
  }
`;

export class SolarScene {
  constructor(canvas, plant) {
    this.plant = plant; this.scene = new THREE.Scene(); this.batches = new Map(); this.nightLights=[];
    this.camera = new THREE.PerspectiveCamera(48,innerWidth/innerHeight,.3,18000);
    this.renderer = new THREE.WebGLRenderer({canvas,antialias:true,powerPreference:'high-performance'});
    this.renderer.setPixelRatio(Math.min(devicePixelRatio,1.5));this.renderer.setSize(innerWidth,innerHeight);
    this.renderer.outputColorSpace=THREE.SRGBColorSpace;this.renderer.toneMapping=THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure=EXPOSURE;this.renderer.shadowMap.enabled=true;this.renderer.shadowMap.type=THREE.PCFSoftShadowMap;
    this.controls=new OrbitControls(this.camera,canvas);this.controls.enableDamping=true;this.controls.dampingFactor=.065;
    this.controls.minDistance=5;this.controls.maxDistance=4000;this.controls.autoRotateSpeed=.16;
    // Allow orbiting past level so the sky, sun and clouds are reachable at
    // all. A polar limit just under 90 degrees keeps the camera above ground
    // but also makes it impossible to ever look up, which hid the sun
    // entirely; eye height is clamped in animate() instead.
    this.controls.maxPolarAngle=Math.PI*.97;
    this.controls.addEventListener('start',()=>{this.transition=null;});
    this.materials={
      steel:new THREE.MeshStandardMaterial({color:0x87918c,metalness:.72,roughness:.42}),
      white:new THREE.MeshStandardMaterial({color:0xd2d5c9,roughness:.65,metalness:.15}),
      dark:new THREE.MeshStandardMaterial({color:0x293430,roughness:.72}),
      concrete:new THREE.MeshStandardMaterial({color:0xb1aa94,roughness:.96}),
      road:new THREE.MeshStandardMaterial({color:0xa29b83,roughness:1}),
      teal:new THREE.MeshStandardMaterial({color:0x52746a,metalness:.3,roughness:.6}),
      insulator:new THREE.MeshStandardMaterial({color:0x665348,metalness:.15,roughness:.32}),
      yellow:new THREE.MeshStandardMaterial({color:0xd8bc58,roughness:.72}),
    };
    this.setupLight();this.setupClouds();this.buildSite();this.flushBatches();this.setView('aerial',true);
    this.godRaysEnabled=true;
    this.onResize=()=>{
      this.camera.aspect=innerWidth/innerHeight;this.camera.updateProjectionMatrix();
      this.renderer.setSize(innerWidth,innerHeight);
      const w=Math.max(1,Math.floor(innerWidth/4)),h=Math.max(1,Math.floor(innerHeight/4));
      this.maskTarget.setSize(w,h);this.raysTarget.setSize(w,h);
      this.godRayMaterial.uniforms.uAspect.value=innerWidth/Math.max(1,innerHeight);
    };
    window.addEventListener('resize',this.onResize);
    canvas.addEventListener('webglcontextlost',e=>{e.preventDefault();document.getElementById('status').textContent='Graphics paused. Reload to restore the scene.';});
    this.animate=this.animate.bind(this);requestAnimationFrame(this.animate);
  }

  box(material,x,y,z,w,h,d,rotation=0) {
    if(!this.batches.has(material))this.batches.set(material,[]);
    const o=new THREE.Object3D();o.position.set(x,y,z);o.scale.set(w,h,d);o.rotation.y=rotation;o.updateMatrix();this.batches.get(material).push(o.matrix.clone());
  }
  beam(a,b,width=.12,material='steel') {
    if(!this.batches.has(material))this.batches.set(material,[]);
    const start=new THREE.Vector3(...a),end=new THREE.Vector3(...b),o=new THREE.Object3D();
    o.position.copy(start).add(end).multiplyScalar(.5);o.scale.set(width,start.distanceTo(end),width);
    o.quaternion.setFromUnitVectors(new THREE.Vector3(0,1,0),end.sub(start).normalize());o.updateMatrix();this.batches.get(material).push(o.matrix.clone());
  }
  flushBatches() {
    for(const [key,matrices] of this.batches){const mesh=new THREE.InstancedMesh(new THREE.BoxGeometry(1,1,1),this.materials[key],matrices.length);
      matrices.forEach((matrix,i)=>mesh.setMatrixAt(i,matrix));mesh.castShadow=key!=='road';mesh.receiveShadow=true;mesh.computeBoundingSphere();this.scene.add(mesh);}
    this.batches.clear();
  }

  setupLight() {
    this.sky=new Sky();this.sky.scale.setScalar(12000);this.sky.layers.set(LAYER_SKY);this.scene.add(this.sky);
    // An explicit sun, in two parts: the atmospheric sky alone renders only a
    // faint bright patch, and the god-ray mask needs something to emit.
    // Glare first, so the disc draws over it.
    this.sunGlow=new THREE.Sprite(new THREE.SpriteMaterial({
      map:sunGlowTexture(),transparent:true,depthWrite:false,
      blending:THREE.AdditiveBlending,fog:false,
    }));
    this.sunGlow.layers.set(LAYER_SKY);this.scene.add(this.sunGlow);
    // Normal blending, not additive. The photosphere is opaque and vastly
    // brighter than the sky behind it, so the disc occludes that sky rather
    // than adding to it. It has to: the atmospheric model draws its own
    // saturated white spot exactly where the sun is, and anything added on top
    // of an already-clipped pixel keeps no colour at all - which is why the
    // setting sun rendered white however red its own colour was set.
    this.sunSprite=new THREE.Sprite(new THREE.SpriteMaterial({
      map:sunDiscTexture(),transparent:true,depthWrite:false,fog:false,
    }));
    // Parked SUN_DISTANCE_M from the eye, so scale is very nearly the angular
    // size in radians times that distance. 100 m at 9 km is 0.64 degrees,
    // against the real sun's 0.53 - a slight exaggeration so the disc survives
    // at modest window sizes, not a blob standing in for one.
    this.sunSprite.scale.setScalar(SUN_DISC_SCALE);this.sunSprite.layers.set(LAYER_SKY);
    this.sunSprite.renderOrder=2;
    this.scene.add(this.sunSprite);
    // The visible sun is only a few pixels across in the quarter-resolution
    // mask, and its glow texture is mostly falloff - far too little signal to
    // smear into rays. The mask gets its own broad, flat-white emitter.
    this.maskSun=new THREE.Sprite(new THREE.SpriteMaterial({
      map:canvasTexture(128,128,(c,w,h)=>{
        const g=c.createRadialGradient(w/2,h/2,0,w/2,h/2,w/2);
        g.addColorStop(0,'rgba(255,255,255,1)');
        g.addColorStop(0.5,'rgba(255,255,255,1)');
        g.addColorStop(1,'rgba(255,255,255,0)');
        c.fillStyle=g;c.fillRect(0,0,w,h);
      }),
      transparent:true,depthWrite:false,depthTest:false,fog:false,
    }));
    // Wide enough to survive the quarter-resolution mask (2.7 degrees is
    // about 14 px there, where the 0.64 degree disc would be 3) but no wider,
    // because everything inside this radius smears into an even wash rather
    // than into rays.
    this.maskSun.scale.setScalar(470);this.maskSun.layers.set(LAYER_MASK_SUN);
    this.scene.add(this.maskSun);
    Object.assign(this.sky.material.uniforms.turbidity,{value:3.5});this.sky.material.uniforms.rayleigh.value=1.8;
    // Mie coefficient and eccentricity are set per snapshot in updateSnapshot,
    // because together they decide whether the sun is visible at all.
    this.sun=new THREE.DirectionalLight(0xffe2b8,3.1);this.sun.castShadow=true;this.sun.shadow.mapSize.set(4096,4096);
    this.sun.shadow.camera.near=10;this.sun.shadow.camera.far=5500;this.sun.shadow.bias=-.00015;this.sun.shadow.normalBias=.11;
    this.scene.add(this.sun,this.sun.target);
    this.hemi=new THREE.HemisphereLight(0xc6d9e6,0x75613e,1.4);this.scene.add(this.hemi);
    this.scene.fog=new THREE.FogExp2(0xb5b6a0,.00012);
    this.sunDirection=new THREE.Vector3(-.8,.5,.3).normalize();
    // An environment from the atmospheric sky gives glass a real reflection
    // response. Regenerate only when the selected weather snapshot changes.
    this.pmrem=new THREE.PMREMGenerator(this.renderer);this.environmentScene=new THREE.Scene();
    this.environmentSky=new Sky();this.environmentSky.scale.setScalar(500);this.environmentScene.add(this.environmentSky);
    const positions=[];for(let i=0;i<650;i++){const a=random()*Math.PI*2,y=random(),r=9000;positions.push(r*Math.sqrt(1-y*y)*Math.cos(a),r*y,r*Math.sqrt(1-y*y)*Math.sin(a));}
    const geometry=new THREE.BufferGeometry();geometry.setAttribute('position',new THREE.Float32BufferAttribute(positions,3));
    this.stars=new THREE.Points(geometry,new THREE.PointsMaterial({color:0xc2d2e3,size:1.1,sizeAttenuation:false,transparent:true,opacity:0,depthWrite:false}));
    this.stars.layers.set(LAYER_SKY);this.scene.add(this.stars);
    this.camera.layers.enable(LAYER_CLOUD);this.camera.layers.enable(LAYER_SKY);
    this.setupGodRays();
  }

  setupGodRays() {
    // Quarter resolution: the mask is radially blurred anyway, and this pass
    // redraws the solid geometry, so full resolution would be wasteful.
    const width=Math.max(1,Math.floor(innerWidth/4)),height=Math.max(1,Math.floor(innerHeight/4));
    this.maskTarget=new THREE.WebGLRenderTarget(width,height,{depthBuffer:true});
    this.raysTarget=new THREE.WebGLRenderTarget(width,height,{depthBuffer:false});
    this.occluderMaterial=new THREE.MeshBasicMaterial({color:0x000000,fog:false});
    this.godRayMaterial=new THREE.ShaderMaterial({
      uniforms:{
        tMask:{value:this.maskTarget.texture},uSunPos:{value:new THREE.Vector2(.5,.5)},
        // Tuned by measuring how far added light actually reaches from the
        // sun; weaker settings faded out within a few degrees of the disc.
        // Measured with the sun centred in view: at weight .22 / exposure 1
        // the composite pinned every pixel out to 20 degrees at 255, which is
        // what was actually washing the sun out - the disc and its glare were
        // being drawn correctly underneath a white sheet. At these values, and
        // with the sun in view under 28 percent cloud, the pass still lifts
        // most of the frame (max +36 levels, median +5) while newly saturating
        // only a handful of pixels. Under heavy overcast it contributes
        // nothing, which is correct: the sun really is blocked.
        // Enough to read as shafts through broken cloud without turning the
        // sky around the sun into a flat white sheet. Measured on a clear sky
        // looking straight at the sun: the pass lifts 3 degrees out by about
        // 40 levels and 8 degrees out by 10, against a 200-level sky.
        uDensity:{value:1},uWeight:{value:.09},uDecay:{value:.97},uExposure:{value:.38},
        // Half the disc's angular size as a fraction of the viewport's height,
        // which is what the fragment shader measures distances in.
        uSunRadius:{value:SUN_DISC_SCALE/2/SUN_DISTANCE_M*(180/Math.PI)/48},
        uAspect:{value:innerWidth/Math.max(1,innerHeight)},
      },
      vertexShader:FULLSCREEN_VERTEX_SHADER,fragmentShader:GODRAY_FRAGMENT_SHADER,
      depthTest:false,depthWrite:false,
    });
    // Uses the same camera-independent fullscreen vertex shader as the blur;
    // a MeshBasicMaterial quad gets clipped by the ortho camera's near plane.
    this.compositeMaterial=new THREE.ShaderMaterial({
      uniforms:{tRays:{value:this.raysTarget.texture}},
      vertexShader:FULLSCREEN_VERTEX_SHADER,
      fragmentShader:`
        uniform sampler2D tRays;
        varying vec2 vUv;
        void main(){ gl_FragColor = vec4(texture2D(tRays, vUv).rgb, 1.0); }
      `,
      blending:THREE.AdditiveBlending,depthTest:false,depthWrite:false,transparent:true,
    });
    const quad=new THREE.PlaneGeometry(2,2);
    this.godRayScene=new THREE.Scene();this.godRayScene.add(new THREE.Mesh(quad,this.godRayMaterial));
    this.compositeScene=new THREE.Scene();this.compositeScene.add(new THREE.Mesh(quad,this.compositeMaterial));
    this.fullscreenCamera=new THREE.OrthographicCamera(-1,1,1,-1,0,1);
    this.sunScreen=new THREE.Vector3();
  }

  // Sun white, solid geometry black, clouds black-with-their-own-alpha; then
  // smear it radially outwards from the sun. Returns false when the sun is
  // off screen or below the horizon, so the composite can be skipped.
  renderGodRays() {
    const sunDistance=9000;
    this.sunScreen.copy(this.camera.position).addScaledVector(this.sunDirection,sunDistance).project(this.camera);
    const onScreen=this.sunScreen.z<1&&Math.abs(this.sunScreen.x)<1.6&&Math.abs(this.sunScreen.y)<1.6;
    if(!this.godRaysEnabled||!onScreen||this.sunDirection.y<=0.02)return false;

    const previousBackground=this.scene.background;
    this.scene.background=null;
    this.renderer.setRenderTarget(this.maskTarget);
    this.renderer.setClearColor(0x000000,1);this.renderer.clear();
    this.renderer.autoClear=false;

    this.camera.layers.set(LAYER_MASK_SUN);
    this.renderer.render(this.scene,this.camera);
    this.camera.layers.set(LAYER_SOLID);
    this.scene.overrideMaterial=this.occluderMaterial;
    this.renderer.render(this.scene,this.camera);
    this.scene.overrideMaterial=null;
    if(this.clouds.visible){
      this.camera.layers.set(LAYER_CLOUD);
      this.cloudMaterial.uniforms.uOcclusionMode.value=1;
      this.renderer.render(this.scene,this.camera);
      this.cloudMaterial.uniforms.uOcclusionMode.value=0;
    }

    this.renderer.autoClear=true;
    this.camera.layers.set(LAYER_SOLID);
    this.camera.layers.enable(LAYER_CLOUD);this.camera.layers.enable(LAYER_SKY);
    this.scene.background=previousBackground;

    this.godRayMaterial.uniforms.uSunPos.value.set(this.sunScreen.x*.5+.5,this.sunScreen.y*.5+.5);
    this.renderer.setRenderTarget(this.raysTarget);
    this.renderer.render(this.godRayScene,this.fullscreenCamera);
    this.renderer.setRenderTarget(null);
    return true;
  }

  setupClouds() {
    // Two altitude bands standing in for real per-height cloud data - see the
    // comment above CLOUD_DOMAIN_M. Voxel counts are sized so each layer is
    // ~78 m per voxel horizontally (a 1-2 km cloud spans 15-25 voxels, enough
    // for a rounded silhouette under trilinear filtering) and stay under a
    // megabyte in total at one byte of density per voxel.
    const low={base:800,top:1900}, high={base:4600,top:5500};
    this.cloudLayers={low,high};
    const started=performance.now();
    // 64 levels over a 1500 m band is ~23 m per voxel vertically. The old 32
    // were ~47 m, which was coarser than the features it was trying to hold -
    // a cauliflower top is a 20-50 m structure, so it simply averaged away.
    const lowVolume=bakeCloudVolume(128,64,128,
      {cells:7,erosion:.30,billowCells:13,baseSharpness:.10,widestAt:.44,lean:8,
       minTower:.34,maxTower:.9,seed:11});
    // Cirrus-like: shallow, wide, barely any tower, heavily eroded.
    const highVolume=bakeCloudVolume(64,16,64,
      {cells:4,erosion:.45,billowCells:8,baseSharpness:.3,widestAt:.5,lean:3,
       minTower:.6,maxTower:.95,seed:47});
    this.cloudBakeMs=performance.now()-started;
    this.cloudMaterial=new THREE.ShaderMaterial({
      glslVersion:THREE.GLSL3, // sampler3D needs GLSL ES 3.00
      uniforms:{
        uTime:{value:0},uDriftSpeed:{value:0},uCoverageLow:{value:0},uCoverageHigh:{value:0},
        uDensityScale:{value:.019},uOcclusionMode:{value:0},
        uSunDirection:{value:new THREE.Vector3(0,1,0)},
        uSunColor:{value:new THREE.Color(0xffffff)},uSunIntensity:{value:0},
        uAmbientColor:{value:new THREE.Color(0xffffff)},uAmbientIntensity:{value:0},
        uGroundColor:{value:new THREE.Color(0x8b7d63)},uPowder:{value:.6},uSteps:{value:96},
        uDomain:{value:CLOUD_DOMAIN_M},
        uLowCloudVolume:{value:lowVolume},uHighCloudVolume:{value:highVolume},
        uLowBase:{value:low.base},uLowTop:{value:low.top},
        uHighBase:{value:high.base},uHighTop:{value:high.top},
      },
      vertexShader:CLOUD_VERTEX_SHADER,fragmentShader:CLOUD_FRAGMENT_SHADER,
      transparent:true,depthWrite:false,side:THREE.DoubleSide,
    });
    this.clouds=new THREE.Mesh(new THREE.SphereGeometry(15000,32,16),this.cloudMaterial);
    this.clouds.renderOrder=1;this.clouds.layers.set(LAYER_CLOUD);
    this.scene.add(this.clouds);
  }

  buildSite() {
    const l=this.plant.layout, tableW=l.modules_per_string*1.303,tableD=2.384;
    const cols=5,rows=Math.ceil(l.strings_per_block/cols),rowPitch=6.8,colPitch=tableW+.7;
    const blockW=cols*colPitch,blockD=rows*rowPitch+20;
    const siteCols=Math.ceil(Math.sqrt(l.blocks)),siteRows=Math.ceil(l.blocks/siteCols),gap=22;
    this.width=siteCols*(blockW+gap)-gap;this.depth=siteRows*(blockD+gap)-gap;
    const W=this.width,D=this.depth;this.blockOrigins=[];this.blockBounds=[];
    const soil=soilTexture();soil.repeat.set(190,190);soil.anisotropy=8;
    const groundGeometry=new THREE.PlaneGeometry(12000,12000,160,160);groundGeometry.rotateX(-Math.PI/2);
    const pos=groundGeometry.attributes.position;
    for(let i=0;i<pos.count;i++){const x=pos.getX(i),z=pos.getZ(i),outside=Math.max(0,Math.max(Math.abs(x)-W/2-150,Math.abs(z)-D/2-240));
      const hill=clamp(outside/1300,0,1);pos.setY(i,-.12+hill*(42+36*Math.sin(x*.0018)*Math.cos(z*.0012)+19*Math.sin(x*.006+z*.004)));}
    groundGeometry.computeVertexNormals();const ground=new THREE.Mesh(groundGeometry,new THREE.MeshStandardMaterial({map:soil,roughness:1,color:0xc9c5a8}));ground.receiveShadow=true;this.scene.add(ground);
    this.box('road',0,-.015,D/2+33,W+70,.1,12);this.box('road',-W/2-27,-.02,0,10,.1,D+68);this.box('road',W/2+27,-.02,0,10,.1,D+68);this.box('road',0,-.02,-D/2-27,W+65,.1,10);
    for(let c=1;c<siteCols;c++)this.box('road',-W/2+c*(blockW+gap)-gap/2,-.02,0,7,.1,D+50);
    for(let r=1;r<siteRows;r++)this.box('road',0,-.02,-D/2+r*(blockD+gap)-gap/2,W+50,.1,8);

    const tilt=THREE.MathUtils.degToRad(l.tilt_deg),az=THREE.MathUtils.degToRad(l.azimuth_deg)-Math.PI;
    const rotation=new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0,1,0),az)
      .multiply(new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1,0,0),tilt));
    const count=l.blocks*l.strings_per_block,tex=moduleTexture();tex.repeat.set(l.modules_per_string,1);tex.anisotropy=16;
    this.panelMaterial=new THREE.MeshPhysicalMaterial({map:tex,color:0xffffff,metalness:.16,roughness:.27,clearcoat:1,clearcoatRoughness:.18,envMapIntensity:.7});
    const geo=new THREE.PlaneGeometry(tableW-.03,tableD-.03);geo.rotateX(-Math.PI/2);
    const panels=new THREE.InstancedMesh(geo,this.panelMaterial,count);panels.castShadow=true;panels.receiveShadow=true;
    const backs=new THREE.InstancedMesh(new THREE.BoxGeometry(tableW,.06,tableD),this.materials.steel,count);backs.castShadow=true;
    const o=new THREE.Object3D(),local=new THREE.Vector3();let index=0;
    for(let block=0;block<l.blocks;block++) {
      const bx=-W/2+(block%siteCols)*(blockW+gap)+blockW/2,bz=-D/2+Math.floor(block/siteCols)*(blockD+gap)+blockD/2;
      this.blockOrigins.push({x:bx,z:bz});
      this.blockBounds.push({id:`BLK-${String(block+1).padStart(3,'0')}`,x:bx,z:bz,w:blockW,d:blockD});
      for(let t=0;t<l.strings_per_block;t++){
        const x=bx-blockW/2+(t%cols)*colPitch+tableW/2,z=bz-blockD/2+Math.floor(t/cols)*rowPitch+tableD/2;
        o.position.set(x,1.95,z);o.quaternion.copy(rotation);o.updateMatrix();backs.setMatrixAt(index,o.matrix);
        local.set(0,.035,0).applyQuaternion(rotation);o.position.add(local);o.updateMatrix();panels.setMatrixAt(index,o.matrix);index++;
        // Piles and front/back galvanized rails stay visible at ground level.
        for(const sx of [-tableW*.4,0,tableW*.4])for(const sz of [-.82,.82]){
          local.set(sx,0,sz).applyQuaternion(rotation);const top=1.90+local.y;
          this.box('steel',x+local.x,top/2,z+local.z,.11,top,.11);
        }
      }
      const sz=bz+blockD/2-9;this.box('concrete',bx,.13,sz,20,.3,12);
      this.box('white',bx,1.75,sz,10,3.2,3.0);this.box('teal',bx,3.4,sz,10.4,.16,3.4);
      for(let k=0;k<11;k++)this.box('dark',bx-4.5+k*.86,1.9,sz+1.51,.5,1.7,.05);
      this.box('teal',bx+7,1.4,sz,3.2,2.4,3.2);
      for(let k=0;k<8;k++)this.box('steel',bx+5.4+k*.45,1.45,sz+1.75,.07,2.3,.45);
      this.sign(`INV-${String(block+1).padStart(3,'0')}`,'5 MW / 33 kV',bx,2.8,sz+1.56,2.5,.78);
    }
    panels.computeBoundingSphere();backs.computeBoundingSphere();this.scene.add(panels,backs);this.panels=panels;
    this.fence(-W/2-40,-D/2-40,W+80,D+100);
    this.substation={x:W/2-100,z:D/2+145};this.buildSubstation();this.buildLandscape();
    this.setupBlockOverlay();
  }

  sign(text,sub,x,y,z,w=5,h=1.6){const mesh=new THREE.Mesh(new THREE.PlaneGeometry(w,h),new THREE.MeshStandardMaterial({map:labelTexture(text,sub),roughness:.85}));mesh.position.set(x,y,z);this.scene.add(mesh);}

  fence(x,z,w,d) {
    const vertices=[];
    for(const [a,b] of [[[x,z],[x+w,z]],[[x+w,z],[x+w,z+d]],[[x+w,z+d],[x,z+d]],[[x,z+d],[x,z]]]) {
      const length=Math.hypot(b[0]-a[0],b[1]-a[1]),n=Math.ceil(length/4);
      for(let i=0;i<=n;i++){const t=i/n,px=a[0]+(b[0]-a[0])*t,pz=a[1]+(b[1]-a[1])*t;this.box('steel',px,1.25,pz,.07,2.5,.07);}
      for(const y of [.35,.9,1.45,2,2.4])vertices.push(a[0],y,a[1],b[0],y,b[1]);
      const meshN=Math.ceil(length/.65);for(let i=0;i<meshN;i++){const t=i/meshN,t2=(i+1)/meshN;vertices.push(a[0]+(b[0]-a[0])*t,.3,a[1]+(b[1]-a[1])*t,a[0]+(b[0]-a[0])*t2,2.25,a[1]+(b[1]-a[1])*t2);}
    }
    const g=new THREE.BufferGeometry();g.setAttribute('position',new THREE.Float32BufferAttribute(vertices,3));this.scene.add(new THREE.LineSegments(g,new THREE.LineBasicMaterial({color:0x8e9b8f,transparent:true,opacity:.55})));
  }

  buildSubstation() {
    const {x,z}=this.substation;this.box('road',x,-.02,z,200,.12,150);this.box('road',x,-.015,z-80,9,.13,60);
    this.fence(x-100,z-75,200,150);
    // Control building with clerestory windows and roof-mounted HVAC.
    this.box('concrete',x-65,.22,z+28,32,.5,21);this.box('white',x-65,3.2,z+28,30,6,19);this.box('teal',x-65,6.3,z+28,31,.3,20);
    for(let i=0;i<6;i++)this.box('dark',x-76+i*4.5,3.4,z+37.52,2.4,2,.05);
    for(let i=0;i<3;i++)this.box('steel',x-72+i*7,6.9,z+28,3,1.1,3);
    this.sign('PAVAGADA','100 MW / CONTROL ROOM',x-65,5.2,z+37.56,13,2.5);
    this.box('concrete',x+4,.45,z+10,43,.9,26);
    for(const offset of [-12,12]){
      this.box('teal',x+offset,3.3,z+10,9,5.8,12);
      for(let i=0;i<13;i++){this.box('steel',x+offset-4.8,3.1,z+4.4+i*.88,.7,4.6,.17);this.box('steel',x+offset+4.8,3.1,z+4.4+i*.88,.7,4.6,.17);}
      this.box('teal',x+offset,7,z+10,8,1,3);
      for(let phase=0;phase<3;phase++){
        const px=x+offset-2.8+phase*2.8;this.box('insulator',px,8,z+6,.5,4,.5);
        for(let ring=0;ring<12;ring++)this.box('insulator',px,6.2+ring*.3,z+6,.9,.11,.9);
      }
    }
    for(let bay=0;bay<4;bay++){
      const bx=x-30+bay*28;
      for(const gz of [z-48,z-18]){
        for(const dx of [-10,10]){
          this.box('concrete',bx+dx,.3,gz,2,.6,2);this.box('steel',bx+dx,7,gz,.35,14,.35);
          this.beam([bx+dx-1.2,0,gz],[bx+dx,10,gz],.13);
        }
        this.box('steel',bx,13.8,gz,22,.45,.45);
        for(let phase=-1;phase<=1;phase++){
          const px=bx+phase*5;this.box('insulator',px,12.5,gz,.35,2.5,.35);
          for(let ring=0;ring<8;ring++)this.box('insulator',px,11.4+ring*.28,gz,.65,.1,.65);
          this.box('concrete',px,.35,gz+6,1.8,.7,1.8);this.box('steel',px,2,gz+6,.4,3.4,.4);this.box('insulator',px,4.4,gz+6,.55,2,.55);
          this.beam([px,5.5,gz+6],[px,11.2,gz],.045,'dark');
        }
      }
      for(let phase=-1;phase<=1;phase++)this.wire([bx+phase*5,11.2,z-48],[bx+phase*5,11.2,z-18],1.3);
    }
    // Transmission towers and suspended conductors carry the scale into the horizon.
    for(let t=0;t<3;t++){
      const tx=x+65+t*190,tz=z-130-t*140;
      for(let side=-1;side<=1;side+=2){this.beam([tx+side*5,0,tz-3],[tx+side*1.5,34,tz],.24);this.beam([tx+side*5,0,tz+3],[tx+side*1.5,34,tz],.24);}
      for(let level=0;level<6;level++){const h=level*5,s=5-level*.55;this.beam([tx-s,h,tz],[tx+s,h+5,tz],.15);this.beam([tx+s,h,tz],[tx-s,h+5,tz],.15);}
      for(const h of [24,31]){this.box('steel',tx,h,tz,21,.35,.35);this.beam([tx,34,tz],[tx-10,h,tz],.16);this.beam([tx,34,tz],[tx+10,h,tz],.16);}
      if(t<2)for(const dx of [-9,0,9])this.wire([tx+dx,30,tz],[tx+190+dx,30,tz-140],8);
    }
    // White service pickup: a familiar reference for the site's physical scale.
    const vx=x-36,vz=z+47;this.box('dark',vx,.5,vz,2,.4,4.7);this.box('white',vx,1.1,vz,2,.9,4.7);this.box('white',vx,1.95,vz-.6,1.9,.8,2.1);
    this.box('dark',vx,1.97,vz+.46,1.65,.52,.06);this.box('dark',vx,1.6,vz+1.5,1.65,.08,1.4);
    for(const dx of [-1.05,1.05])for(const dz of [-1.55,1.55])this.box('dark',vx+dx,.55,vz+dz,.3,.78,.78);
  }

  wire(a,b,sag){const pts=[];for(let i=0;i<=20;i++){const t=i/20;pts.push(new THREE.Vector3(a[0]+(b[0]-a[0])*t,a[1]+(b[1]-a[1])*t-sag*4*t*(1-t),a[2]+(b[2]-a[2])*t));}
    this.scene.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts),new THREE.LineBasicMaterial({color:0x354037})));}

  buildLandscape() {
    const geometry=new THREE.IcosahedronGeometry(1,1),mat=new THREE.MeshStandardMaterial({color:0x627448,roughness:1});
    const trees=new THREE.InstancedMesh(geometry,mat,1700),o=new THREE.Object3D();
    for(let n=0;n<1700;n++){
      let x,z;do{x=(random()-.5)*(this.width+1700);z=(random()-.5)*(this.depth+1500);}while(Math.abs(x)<this.width/2+70&&Math.abs(z)<this.depth/2+250);
      const outside=Math.max(0,Math.max(Math.abs(x)-this.width/2-150,Math.abs(z)-this.depth/2-240)),hill=clamp(outside/1300,0,1);
      const y=-.12+hill*(42+36*Math.sin(x*.0018)*Math.cos(z*.0012)+19*Math.sin(x*.006+z*.004));
      const size=1.2+random()*4; o.position.set(x,y+size*.75,z);o.scale.set(size,size*.7,size*.85);o.rotation.set(random(),random()*6,random()*.3);o.updateMatrix();trees.setMatrixAt(n,o.matrix);
      trees.setColorAt(n,new THREE.Color().setHSL(.18+random()*.08,.18+random()*.15,.2+random()*.12));
      if(size>3)this.box('dark',x,y+1.4,z,.25,2.8,.25);
    }trees.castShadow=true;trees.receiveShadow=true;trees.computeBoundingSphere();this.scene.add(trees);
  }

  // ---- Per-block status overlay -------------------------------------
  // Until now every block was drawn identically, because the model computed
  // one number and repeated it. The backend now simulates each block under
  // its own state, so the site can finally show which one is in trouble.

  setupBlockOverlay() {
    const pad=new THREE.PlaneGeometry(1,1);pad.rotateX(-Math.PI/2);
    this.blockGroup=new THREE.Group();this.blockGroup.renderOrder=2;
    this.blockPads=[];this.blockBeacons=[];this.pickTargets=[];
    for(const bounds of this.blockBounds){
      // depthTest stays ON: these are large ground-level planes, and without
      // it they paint straight over the whole scene from any angle.
      const material=new THREE.MeshBasicMaterial({color:0x3fbf7f,transparent:true,opacity:.12,
        depthWrite:false,side:THREE.DoubleSide});
      const mesh=new THREE.Mesh(pad,material);
      mesh.position.set(bounds.x,.6,bounds.z);mesh.scale.set(bounds.w,1,bounds.d);
      mesh.userData.blockId=bounds.id;this.blockPads.push(mesh);this.pickTargets.push(mesh);
      // A vertical beacon so a faulted block is findable from 700 m up, where
      // a ground tint is nearly edge-on and almost invisible.
      const beacon=new THREE.Mesh(new THREE.CylinderGeometry(3.2,3.2,1,12,1,true),
        new THREE.MeshBasicMaterial({color:0x3fbf7f,transparent:true,opacity:.42,
          depthWrite:false,side:THREE.DoubleSide}));
      beacon.position.set(bounds.x,0,bounds.z);beacon.userData.blockId=bounds.id;
      this.blockBeacons.push(beacon);
      this.blockGroup.add(mesh,beacon);
    }
    const ring=new THREE.RingGeometry(.48,.5,4,1);ring.rotateX(-Math.PI/2);ring.rotateY(Math.PI/4);
    this.selectionRing=new THREE.Mesh(ring,new THREE.MeshBasicMaterial(
      {color:0xffffff,transparent:true,opacity:.9,depthWrite:false,depthTest:false,side:THREE.DoubleSide}));
    this.selectionRing.visible=false;this.selectionRing.renderOrder=3;
    this.blockGroup.add(this.selectionRing);
    this.scene.add(this.blockGroup);
    this.raycaster=new THREE.Raycaster();this.pointer=new THREE.Vector2();
    this.overlayVisible=true;this.selectedBlockId=null;
  }

  // Green when a block matches its peers, amber when it is behind them, red
  // when it is dark, blue when it reads high. Colour is the residual, not the
  // raw output, so a cloudy hour does not turn the whole site amber.
  blockColor(block) {
    if(block.flag==='not_producing')return new THREE.Color(0xf05a4a);
    if(block.flag==='underperforming')return new THREE.Color(0xe8a33d);
    if(block.flag==='above_model')return new THREE.Color(0x63b6f0);
    return new THREE.Color(0x3fbf7f);
  }

  updateFleet(blocks,capacityMw) {
    if(!this.blockPads||!blocks)return;
    const limit=capacityMw||5;
    blocks.forEach((block,index)=>{
      const pad=this.blockPads[index],beacon=this.blockBeacons[index];
      if(!pad||!beacon)return;
      const color=this.blockColor(block),share=clamp(block.inverter_ac_mw/limit,0,1);
      const faulted=block.flag!=='normal';
      pad.material.color.copy(color);
      pad.material.opacity=faulted?.42:.11+share*.17;
      // Beacon height tracks output, so the site reads as a bar chart from
      // above; a faulted block gets a fixed tall marker instead so that a
      // dark block is conspicuous rather than invisible.
      const height=faulted?150:20+share*110;
      beacon.scale.set(1,height,1);beacon.position.y=height/2;
      beacon.material.color.copy(color);
      beacon.material.opacity=faulted?.6:.15+share*.2;
    });
    if(this.selectedBlockId)this.highlightBlock(this.selectedBlockId);
  }

  setOverlayVisible(visible) {
    this.overlayVisible=visible;
    if(this.blockGroup)this.blockGroup.visible=visible;
  }

  highlightBlock(blockId) {
    this.selectedBlockId=blockId;
    const bounds=this.blockBounds.find(b=>b.id===blockId);
    if(!bounds||!this.selectionRing){if(this.selectionRing)this.selectionRing.visible=false;return;}
    this.selectionRing.visible=true;
    this.selectionRing.position.set(bounds.x,.9,bounds.z);
    this.selectionRing.scale.set(bounds.w*1.08,1,bounds.d*1.08);
  }

  clearHighlight(){this.selectedBlockId=null;if(this.selectionRing)this.selectionRing.visible=false;}

  // Returns the block id under the pointer, or null.
  pickBlock(clientX,clientY) {
    if(!this.raycaster||!this.pickTargets)return null;
    const rect=this.renderer.domElement.getBoundingClientRect();
    this.pointer.set((clientX-rect.left)/rect.width*2-1,-((clientY-rect.top)/rect.height)*2+1);
    this.raycaster.setFromCamera(this.pointer,this.camera);
    const hits=this.raycaster.intersectObjects(this.pickTargets,false);
    return hits.length?hits[0].object.userData.blockId:null;
  }

  focusBlock(blockId) {
    const bounds=this.blockBounds.find(b=>b.id===blockId);
    if(!bounds)return;
    this.view='block';this.controls.autoRotate=false;
    this.transition={start:performance.now(),
      fromEye:this.camera.position.clone(),fromTarget:this.controls.target.clone(),
      eye:new THREE.Vector3(bounds.x-bounds.w*.55,Math.max(bounds.w,bounds.d)*.62,bounds.z+bounds.d*.72),
      target:new THREE.Vector3(bounds.x,2,bounds.z)};
  }

  setView(name,instant=false) {
    this.view=name;const W=this.width,D=this.depth,s=this.substation,b=this.blockOrigins[this.plant.layout.blocks-1];
    const d=this.sunDirection;
    const views={
      aerial:{eye:[W*.8,680,D*.78],target:[0,0,D*.12]},
      rows:{eye:[b.x-58,4.2,b.z+3],target:[b.x+90,2.3,b.z-65]},
      station:{eye:[s.x-73,24,s.z+86],target:[s.x+2,4,s.z-2]},
      // OrbitControls always looks AT the target, so the only way to look up
      // is to put the target overhead - orbiting from a ground-level target
      // just swings you over the top looking down. This aims at the sun.
      sky:{eye:[0,18,D*.3],target:[d.x*2500,18+d.y*2500,D*.3+d.z*2500]},
    };const v=views[name];this.controls.autoRotate=false;
    const eye=new THREE.Vector3(...v.eye),target=new THREE.Vector3(...v.target);
    if(instant||matchMedia('(prefers-reduced-motion: reduce)').matches){
      this.camera.position.copy(eye);this.controls.target.copy(target);this.transition=null;this.controls.update();
      // OrbitControls.update() recomposes camera.matrix (position+quaternion)
      // but does not itself push that into matrixWorld. Rendering's own
      // implicit auto-update isn't reliably in effect for the very first
      // frame after an instant (non-animated) view change, which left
      // matrixWorld's rotation stale (identity) while matrix was already
      // correct - the mismatch made frustum culling reject nearly the whole
      // scene, since culling reads matrixWorld. Forcing it here closes that gap.
      this.camera.updateMatrixWorld(true);
    }
    else this.transition={start:performance.now(),fromEye:this.camera.position.clone(),fromTarget:this.controls.target.clone(),eye,target};
  }

  updateSnapshot(snapshot) {
    const zen=snapshot.solar_position.zenith_deg,azi=snapshot.solar_position.azimuth_deg,el=90-zen;
    const r=THREE.MathUtils.degToRad,day=clamp((el+5)/18,0,1),warm=clamp((22-el)/22,0,1);
    this.sunDirection.set(Math.cos(r(el))*Math.sin(r(azi)),Math.sin(r(el)),-Math.cos(r(el))*Math.cos(r(azi)));
    const sky=this.sky.material.uniforms;sky.sunPosition.value.copy(this.sunDirection);
    sky.turbidity.value=2.4+snapshot.cloud_cover_fraction*3.2;
    // Mie scattering sets the size and brightness of the aureole - the haze
    // halo around the sun - and so decides whether the sun can be seen at all.
    // At three.js's default 0.005 the sky one degree from the disc rendered at
    // 245/255 while the disc clipped at 255: four percent contrast, which is
    // why the sun was invisible however bright the sprite was made. Lowering
    // it opens that headroom, at a measured cost of six levels of ground
    // brightness. It climbs again as the sun drops, which is what makes a
    // sunset glow, and stands in crudely for the far longer path length
    // through the aerosol layer at low elevation.
    const lowSun=clamp((25-el)/25,0,1);
    sky.mieCoefficient.value=.0022+lowSun*.002;
    sky.mieDirectionalG.value=.62+lowSun*.08;
    this.sky.visible=el>-7;this.stars.material.opacity=clamp((-el-5)/15,0,.85);
    // The disc stays white until the sun is genuinely low; the glare around it
    // grows, warms and strengthens as the light travels further through the
    // atmosphere. Both go out below the horizon.
    this.sunSprite.visible=this.sunGlow.visible=el>-1;
    // Physical extinction rather than a hand-tuned warm lerp: the disc takes
    // whatever colour the atmosphere actually leaves it at this elevation,
    // which runs warm white overhead, gold by 15 degrees and deep red on the
    // horizon without any of those being chosen by hand.
    const extinction=sunExtinction(el);
    this.sunSprite.material.color.setRGB(
      SUN_PEAK_RADIANCE*extinction[0], SUN_PEAK_RADIANCE*extinction[1], SUN_PEAK_RADIANCE*extinction[2]);
    this.sunSprite.material.opacity=clamp((el+1)/2.5,0,1);
    // Measured radial profile of the glare at this size, over the sky behind
    // it: +43 levels at one degree from the disc, +22 at two, +6 at four,
    // gone by eight. Strong enough to read as glare, tight enough that it does
    // not simply wash the disc away again.
    this.sunGlow.scale.setScalar(1400+lowSun*1500);
    // The aureole takes the extinction's hue but not its dimming. It is light
    // scattered out of the beam along the whole path, so it is at its
    // brightest exactly when the direct disc is at its faintest - which is why
    // a low sun sits in a large orange glow rather than going out quietly.
    const aureolePeak=Math.max(...extinction);
    this.sunGlow.material.color.setRGB(extinction[0]/aureolePeak,
      extinction[1]/aureolePeak, extinction[2]/aureolePeak);
    this.sunGlow.material.opacity=clamp((el+1)/3,0,1)*(1-snapshot.cloud_cover_fraction*.5);
    this.scene.background=new THREE.Color(0x070f1b);
    this.sun.color.set(0xfff1d7).lerp(new THREE.Color(0xffb675),warm);
    this.sun.intensity=day*(2.8-snapshot.cloud_cover_fraction*.8)*LIGHT_GAIN;
    this.hemi.intensity=(.07+day*1.25)*LIGHT_GAIN;
    this.hemi.color.set(0xb9d5e5).lerp(new THREE.Color(0x4e698a),1-day);
    this.scene.fog.color.set(0xbac3b9).lerp(new THREE.Color(0xcab696),warm).lerp(new THREE.Color(0x101d2c),1-day);
    this.scene.fog.density=.00010+(1-day)*.000025;this.renderer.toneMappingExposure=EXPOSURE;
    this.environmentSky.material.uniforms.sunPosition.value.copy(this.sunDirection);
    this.environmentSky.material.uniforms.turbidity.value=sky.turbidity.value;
    if(this.environmentTarget)this.environmentTarget.dispose();
    this.environmentTarget=this.pmrem.fromScene(this.environmentScene,.03,.1,1000);
    this.scene.environment=day>.01?this.environmentTarget.texture:null;
    this.panelMaterial.envMapIntensity=.45*day;
    this.clouds.visible=snapshot.cloud_cover_fraction>0.001;
    const cloud=this.cloudMaterial.uniforms;
    // Only one real cloud_cover figure exists per snapshot (Open-Meteo does
    // not break it out by height); the high (cirrus-like) layer is scaled
    // down from it as an illustrative assumption until real per-height data
    // is available, matching the low layer 1:1 with the real/synthetic value.
    cloud.uCoverageLow.value=snapshot.cloud_cover_fraction;
    cloud.uCoverageHigh.value=snapshot.cloud_cover_fraction*0.5;
    cloud.uSunDirection.value.copy(this.sunDirection);
    // Clouds are lit by the same sun and sky fill as the rest of the scene, so
    // they darken through dusk on their own rather than via a separate fudge.
    // The sun term is scaled up because the phase function spreads its energy
    // over the sphere; ambient stays low so shadowed bases stay readable.
    // The directional light is tinted warm all day so the panels read as
    // sunlit, but a cumulus at midday is white - the warmth belongs to low
    // sun, not to daylight. Lerping towards the warm tint only as the sun
    // drops keeps midday clouds neutral and still turns them orange at dusk.
    cloud.uSunColor.value.set(0xfffaf2).lerp(this.sun.color,warm);
    // The scattering octaves below already sum to roughly twice the energy a
    // single Beer term gave, so the intensity comes down to match; and sky
    // fill goes up, because a 9:0.4 sun-to-sky ratio drove the whole cloud
    // through the warm end of the tone curve and left it yellow.
    // Clouds get their own gain rather than LIGHT_GAIN. They already sat high
    // on the tone curve, so scaling them like the ground saturated them to
    // flat white - measured [249,250,250] with 170 of 247 sampled pixels
    // clipped. Tuned instead against the sunlit tops: [215,222,223], spread 8,
    // nothing clipped, with shadowed sides at [145,153,155].
    cloud.uSunIntensity.value=day*9;
    cloud.uAmbientColor.value.copy(this.hemi.color);
    cloud.uAmbientIntensity.value=(.08+day*.62)*.74;
    // Light bounced off the ground fills the cloud base. Pavagada's soil is
    // warm and reddish, so the underside picks that up rather than going the
    // same cool grey as the top - which is most of what tells the eye which
    // way is up on a cloud. This is the soil's own tone rather than the
    // scene's haze colour: the haze is a stylistic green-grey, and inheriting
    // it tinted every cloud olive once the sky fill was strong enough to
    // matter.
    cloud.uGroundColor.value.set(0xcdc3b6).multiplyScalar(.3+day*.7);
    // Illustrative: the snapshot only carries wind at 10 m, not at cloud
    // altitude (where it is generally stronger), and carries no direction at
    // all - so the field drifts along a fixed bearing at a scaled-down speed.
    cloud.uDriftSpeed.value=snapshot.conditions.wind_speed_10m_m_s*1.5;
  }

  setQuality(high){
    this.renderer.setPixelRatio(Math.min(devicePixelRatio,high?1.5:1));this.renderer.shadowMap.enabled=high;
    // The mask pass redraws the solid geometry, so it is the first thing to
    // drop when the user asks for performance over detail.
    this.godRaysEnabled=high;this.renderer.setSize(innerWidth,innerHeight);
    // Fewer, longer strides through the volume: the cheapest real saving here,
    // since the raymarch dominates the frame whenever cloud fills the view.
    this.cloudMaterial.uniforms.uSteps.value=high?96:52;
  }

  animate(now) {
    requestAnimationFrame(this.animate);if(document.hidden)return;
    this.cloudMaterial.uniforms.uTime.value=now*.001;
    if(this.transition){const t=clamp((now-this.transition.start)/1500,0,1),s=t*t*(3-2*t);this.camera.position.lerpVectors(this.transition.fromEye,this.transition.eye,s);this.controls.target.lerpVectors(this.transition.fromTarget,this.transition.target,s);if(t===1)this.transition=null;}
    this.controls.update();
    // Orbiting up tips the camera below the target; keep it above the ground
    // (and above the panel tables) rather than forbidding the rotation.
    if(this.camera.position.y<MIN_EYE_HEIGHT){
      this.camera.position.y=MIN_EYE_HEIGHT;
      this.camera.lookAt(this.controls.target);
    }
    const target=this.controls.target,extent=clamp(this.camera.position.distanceTo(target)*.7,55,1500);
    this.sun.target.position.copy(target);this.sun.position.copy(target).addScaledVector(this.sunDirection,2500);
    const c=this.sun.shadow.camera;c.left=-extent;c.right=extent;c.top=extent;c.bottom=-extent;c.updateProjectionMatrix();
    const heading=Math.atan2(this.camera.position.x-target.x,this.camera.position.z-target.z);
    document.getElementById('compass-needle').style.transform=`rotate(${-heading*180/Math.PI}deg)`;
    // Keep the sun at a fixed offset from the eye so it reads as infinitely
    // far away instead of sliding past as the camera moves.
    this.sunSprite.position.copy(this.camera.position).addScaledVector(this.sunDirection,SUN_DISTANCE_M);
    this.maskSun.position.copy(this.sunSprite.position);
    this.sunGlow.position.copy(this.sunSprite.position);
    const rays=this.renderGodRays();
    this.renderer.render(this.scene,this.camera);
    if(rays){
      // Without this the composite pass clears the canvas first and the only
      // thing left on screen is the rays.
      this.renderer.autoClear=false;
      this.renderer.render(this.compositeScene,this.fullscreenCamera);
      this.renderer.autoClear=true;
    }
  }
}
