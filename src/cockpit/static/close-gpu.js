"use strict";

// The five GPU closes of CONTROL-17, from the close lab: Burn and Ignite set the card on fire, Snap blows its pixels
// away as dust, Vortex drains them into the key and Blast knocks them out towards you. At the press the copy of the
// card shows with its key springing, as in every close. In the next task the copy is painted into a texture, and from
// the frame after that one WebGL2 canvas over the page draws the card's own pixels and the copy goes. Every particle is
// worked out from its index in the shaders, so a frame is a few uniforms and three or four draw calls. The canvas is on
// the page only while a close plays. The shaders compile while the page is idle after it loads; until they are ready,
// or where WebGL2 does not run, the pick leaves these closes out (`GpuCloses.ready()`).
window.GpuCloses = (() => {
  // Room around the card in the texture, for its hairline ring.
  const PAD = 2;
  const GPU = { cv: null, gl: null, khr: null, P: {}, U: new Map(), maxPt: 0, maxTex: 0, view: [1, 1], dpr: 1, jobs: [], on: false, ready: false, failed: "" };

  const GLSL = `#version 300 es
  precision highp float;
  precision highp int;
  uniform vec2 uView;
  uniform vec4 uRect;
  uniform float uT, uDpr, uFront, uAmp, uMaxPt;
  uniform vec2 uO;
  uniform int uFrom;
  vec4 clipAt(vec2 p) { vec2 v = (uRect.xy + p) / uView; return vec4(v.x * 2. - 1., 1. - v.y * 2., 0., 1.); }
  float hashi(uint x) { x ^= x >> 16; x *= 0x7feb352du; x ^= x >> 15; x *= 0x846ca68bu; x ^= x >> 16; return float(x) / 4294967295.; }
  vec3 mod289(vec3 x) { return x - floor(x * (1. / 289.)) * 289.; }
  vec2 mod289(vec2 x) { return x - floor(x * (1. / 289.)) * 289.; }
  vec3 permute(vec3 x) { return mod289(((x * 34.) + 1.) * x); }
  float snoise(vec2 v) {
    const vec4 C = vec4(.211324865405187, .366025403784439, -.577350269189626, .024390243902439);
    vec2 i = floor(v + dot(v, C.yy));
    vec2 x0 = v - i + dot(i, C.xx);
    vec2 i1 = x0.x > x0.y ? vec2(1., 0.) : vec2(0., 1.);
    vec4 x12 = x0.xyxy + C.xxzz;
    x12.xy -= i1;
    i = mod289(i);
    vec3 p = permute(permute(i.y + vec3(0., i1.y, 1.)) + i.x + vec3(0., i1.x, 1.));
    vec3 m = max(.5 - vec3(dot(x0, x0), dot(x12.xy, x12.xy), dot(x12.zw, x12.zw)), 0.);
    m = m * m; m = m * m;
    vec3 x = 2. * fract(p * C.www) - 1.;
    vec3 h = abs(x) - .5;
    vec3 a0 = x - floor(x + .5);
    m *= 1.79284291400159 - .85373472095314 * (a0 * a0 + h * h);
    vec3 g = vec3(a0.x * x0.x + h.x * x0.y, a0.yz * x12.xz + h.yz * x12.yw);
    return 130. * dot(m, g);
  }
  float fbm(vec2 p) { return .55 * snoise(p) + .28 * snoise(p * 2.03 + 17.1) + .17 * snoise(p * 4.11 - 9.3); }
  // How far the fire has to travel to reach a point of the card (card px): distance from the key, the left edge
  // or the bottom, pushed back by domain-warped noise so the front is ragged and never runs backwards.
  float arrival(vec2 p) {
    float d = uFrom == 0 ? length(p - uO) : uFrom == 1 ? p.x : uRect.w - p.y;
    vec2 w = vec2(snoise(p / 170.), snoise(p / 170. + 7.3));
    return d + uAmp * (.5 + .5 * fbm(p / 80. + w * .8)) + 5. * snoise(p / 13.);
  }
  `;

  // The card itself: burning (from the key, the left edge or the bottom), with scorch, shimmer, char and flames.
  const VS_CARD = `${GLSL}
  uniform vec4 uM;
  out vec2 vP;
  void main() { vec2 c = vec2(gl_VertexID & 1, gl_VertexID >> 1); vP = mix(-uM.xy, uRect.zw + uM.zw, c); gl_Position = clipAt(vP); }`;
  const FS_CARD = `${GLSL}
  uniform sampler2D uTex;
  uniform float uEdge, uFlame, uFade, uRad;
  uniform vec3 uHot, uMid, uCool;
  in vec2 vP;
  out vec4 o;
  float sdBox(vec2 p, vec2 s, float r) { vec2 q = abs(p - s * .5) - s * .5 + r; return length(max(q, 0.)) + min(max(q.x, q.y), 0.) - r; }
  vec3 heat(float g) { g = clamp(g, 0., 1.); return g < .5 ? mix(uCool, uMid, g * 2.) : mix(uMid, uHot, g * 2. - 1.); }
  vec4 over(vec4 a, vec3 c, float k) { return a * (1. - k) + vec4(c * k, k); }
  void main() {
    vec2 S = uRect.zw;
    float sd = sdBox(vP - vec2(${PAD}.), S - vec2(${2 * PAD}.), uRad);
    // Outside the card only the flames draw, and only above it.
    if (sd > 3. && (uFlame == 0. || vP.y > S.y || max(-vP.x, vP.x - S.x) > 10.)) discard;
    float x = uFront - arrival(vP);
    float charW = uEdge * 2.6;
    float inside = 1. - smoothstep(-1., 3., sd);
    vec4 c = vec4(0.);
    if (x < charW + 4. && sd < 1.) {
      // Just ahead of the front the paper shimmers in the heat and browns.
      vec2 wob = vec2(0.);
      if (x > -34. && x < 0.) wob = vec2(snoise(vP / 11. + vec2(0., uT * 7.)), snoise(vP / 11. + vec2(4.7, uT * 7.))) * 1.7 * smoothstep(-34., 0., x);
      c = texture(uTex, clamp((vP + wob) / S, 0., 1.));
      float sc = smoothstep(-24., 0., x);
      c.rgb *= mix(vec3(1.), vec3(.52, .34, .2), sc * sc);
      c.rgb = mix(c.rgb, vec3(.05, .035, .03) * c.a, smoothstep(0., uEdge * .8, x));
      float edge = charW + 3. * snoise(vP / 4.5);
      c *= 1. - smoothstep(edge - 1., edge + 1., x);
    }
    float g = 0.;
    if (abs(x - uEdge * .35) < uEdge * 3.) { float gx = (x - uEdge * .35) / uEdge; g = exp(-gx * gx) * (.8 + .2 * snoise(vec2(vP.x / 26., uT * 9.))); }
    c = over(c, heat(g * 1.1), clamp(g, 0., 1.) * inside);
    if (x > charW) c = over(c, uCool, exp(-(x - charW) / (uEdge * 1.5)) * .22 * inside);
    if (uFlame > 0. && x < 3. && x > -uFlame) {
      float hgt = -x;
      // Tongues: noise stretched upward and scrolling up, cut by how far above the front this pixel is.
      vec2 q = vec2(vP.x / 21., vP.y / 62. + uT * 4.2);
      float n = .5 + .5 * fbm(q + vec2(.7 * fbm(q * .55 + uT * .8), 0.));
      float body = clamp(1. - hgt / uFlame, 0., 1.);
      float side = 1. - smoothstep(-2., 10., max(-vP.x, vP.x - S.x));
      float f = smoothstep(.42, .6, n * (.2 + 1.05 * body)) * sqrt(body) * side;
      c = over(c, heat(.12 + .8 * body * n * n + .1 * body), f * .95);
    }
    o = c * uFade;
  }`;

  // Embers that the fire throws off as it passes, rising and cooling.
  const VS_EMBER = `${GLSL}
  uniform float uSpeed;
  uniform vec3 uHot, uMid, uCool;
  out vec4 vC;
  void main() {
    uint id = uint(gl_VertexID);
    float r1 = hashi(id * 4u + 1u), r2 = hashi(id * 4u + 2u), r3 = hashi(id * 4u + 3u), r4 = hashi(id * 4u + 4u);
    vec2 p0 = vec2(r1, r2) * uRect.zw;
    float lt = uT - arrival(p0) / uSpeed, life = .45 + r3 * .75;
    if (lt < 0. || lt > life) { gl_Position = vec4(2., 2., 0., 1.); gl_PointSize = 0.; return; }
    float u = lt / life;
    vec2 p = p0 + vec2(sin(lt * (3. + r4 * 5.) + r1 * 6.28) * 16. * lt + (r4 - .5) * 70. * lt, -(45. + r3 * 160.) * lt - 80. * lt * lt);
    gl_Position = clipAt(p);
    gl_PointSize = (1.3 + r4 * 2.4) * (1. - u * .55) * 2.6 * uDpr;
    float g = 1. - u;
    vec3 col = g > .5 ? mix(uMid, uHot, g * 2. - 1.) : mix(uCool, uMid, g * 2.);
    float a = smoothstep(0., .06, lt) * (1. - smoothstep(.5, 1., u));
    vC = vec4(col * a, a);
  }`;
  const FS_EMBER = `#version 300 es
  precision highp float;
  in vec4 vC;
  out vec4 o;
  void main() { float d = length(gl_PointCoord - .5) * 2.; float a = pow(max(0., 1. - d), 1.6); o = vC * a; }`;

  // The card as its own pixels: one point per cell of uCell px, each drawing its exact patch of the texture, so
  // until a cell lets go the card is pixel for pixel what was on the page.
  const VS_PIX = `${GLSL}
  uniform float uCell, uMaxD;
  uniform int uCols, uMode;
  uniform vec4 uA, uB;
  uniform vec3 uHot, uMid, uCool, uTint, uSurf;
  uniform sampler2D uTex;
  out vec2 vUV0, vUVs;
  out vec4 vTint;
  out vec3 vMul;
  out float vA, vRound;
  vec2 curl(vec2 p) { float e = .35; return vec2(snoise(p + vec2(0., e)) - snoise(p - vec2(0., e)), snoise(p - vec2(e, 0.)) - snoise(p + vec2(e, 0.))) / (2. * e); }
  void main() {
    uint id = uint(gl_VertexID);
    vRound = 0.;
    vec2 cell = vec2(float(gl_VertexID % uCols), float(gl_VertexID / uCols));
    vec2 base = (cell + .5) * uCell;
    float r1 = hashi(id * 3u + 1u), r2 = hashi(id * 3u + 2u), r3 = hashi(id * 3u + 3u);
    // How far this patch is from plain card surface: letters, tags and the key are ink; paper is not.
    vec4 tc = textureLod(uTex, base / uRect.zw, 0.);
    float ink = clamp(length(tc.rgb - uSurf * tc.a) * 3., 0., 1.);
    vec2 pos = base;
    float s = 1., a = 1.;
    vec4 tint = vec4(0.);
    vec3 mul = vec3(1.);
    if (uMode == 0) {
      // Snap. uA: sweep s, wind px/s, swirl, linger. uB.x: 0 from the key, 1 from the left edge.
      float along = uB.x < .5 ? length(base - uO) / uMaxD : base.x / uRect.z;
      float tr = uA.x * clamp(along + .22 * fbm(base / 34.) + (r1 - .5) * .05, 0., 1.);
      // Paper patches thin out quickly; ink patches carry the drift.
      float lt = uT - tr, life = (.42 + r2 * .5) * uA.w * mix(.4, 1., ink);
      if (lt > 0.) {
        float u = lt / life;
        vec2 wind = uA.y * vec2(.92, -.38) * (.55 + r3 * .9);
        pos += wind * (lt + 1.1 * lt * lt) + curl(base / 90. + vec2(lt * .9, 0.)) * uA.z * 90. * lt + vec2(0., -36. * lt * r1);
        s = mix(1., mix(.2, .5, ink), smoothstep(.05, 1., u));
        a = 1. - smoothstep(mix(0., .35, ink), 1., u);
        tint = vec4(uTint, .45 * ink * smoothstep(0., .5, u));
      }
    } else if (uMode == 1) {
      // Vortex. uA: pull s, lag s, spin rad, -. Rows nearest the key fall in first; each patch heads straight
      // for the key and only curls in the last few dozen px, so the card drains rather than smears.
      vec2 d = base - uO;
      float r0 = length(d) + .001;
      float tr = uA.y * (r0 / uMaxD) + .06 * fbm(base / 60.);
      float u = clamp((uT - tr) / (uA.x * (.97 + .06 * r2)), 0., 1.);
      float e = u * u;
      float r = r0 * (1. - e);
      float ang = atan(d.y, d.x) + uA.z * e * (40. / (r + 40.));
      pos = uO + vec2(cos(ang), sin(ang)) * r;
      pos -= d / r0 * 4. * smoothstep(-.1, 0., uT - tr) * step(u, 0.);
      s = mix(1., .25, e);
      a = 1. - smoothstep(mix(.2, .82, ink), 1., u);
      tint = vec4(uHot, e * e * .9);
    } else if (uMode == 2) {
      // Blast. uA: wave px/s, power, depth, -. A shock front from the key frees each patch as it arrives;
      // neighbouring patches fly together (speed and heading from coherent noise), out and towards you.
      vec2 d = base - uO;
      float r0 = length(d);
      float lt = uT - r0 / uA.x;
      if (lt > -.045 && lt <= 0.) pos += normalize(d + .001) * sin((lt / .045 + 1.) * 3.1416) * 3.5;
      if (lt > 0.) {
        float clump = clamp(.5 + .6 * fbm(base / 26.), 0., 1.);
        float th = (clump - .5) * .6 + (r1 - .5) * .12;
        vec2 dir = mat2(cos(th), sin(th), -sin(th), cos(th)) * normalize(d + .001);
        float k = 3.4, f = (1. - exp(-k * lt)) / k;
        float sp = uA.y * (240. + 560. * clump + 100. * r3) * (1.15 - .5 * r0 / uMaxD);
        vec2 p2 = base + dir * sp * f + vec2(0., 170. * lt * lt);
        float z = uA.z * min(320., (80. + 380. * clump) * f * 2.6);
        float persp = 900. / (900. - z);
        vec2 C = uRect.zw * .5;
        pos = C + (p2 - C) * persp;
        s = persp * (1. - .3 * smoothstep(0., .6, lt));
        a = (1. - smoothstep(mix(.04, .16, ink), mix(.3, .58, ink), lt * (.8 + .4 * r2))) * (1. - smoothstep(220., 320., z));
        tint = vec4(mix(vec3(1.), uHot, .4), exp(-lt * 14.) * .8);
      }
    } else {
      // Cinders. uA: front speed px/s, ember share, -, -. The burn shader draws the card up to the front; behind
      // it the letters lift off as glowing char flakes and a few paper patches fly up as embers.
      float lt = uT - arrival(base) / uA.x;
      vec3 hot = vec3(0.);
      if (lt <= 0.) a = 0.;
      else if (ink > .25) {
        float life = .55 + r2 * .5, u = lt / life;
        pos += vec2(sin(lt * (3. + r3 * 4.) + r2 * 6.28) * 10. * lt + (r3 - .5) * 44. * lt, -(30. + r3 * 110.) * lt - 90. * lt * lt) + curl(base / 60. + lt) * 26. * lt;
        float g = exp(-lt * 8.);
        hot = g > .5 ? mix(uMid, uHot, g * 2. - 1.) : mix(uCool, uMid, g * 2.);
        mul = mix(vec3(1.), vec3(.12, .09, .07), smoothstep(0., .18, lt));
        tint = vec4(hot, g * .95);
        s = mix(1., .4, u);
        a = 1. - smoothstep(.4, 1., u);
      } else if (r1 < uA.y) {
        float life = .45 + r2 * .7, u = lt / life;
        pos += vec2(sin(lt * (4. + r3 * 4.) + r2 * 6.28) * 14. * lt + (r3 - .5) * 60. * lt, -(50. + r3 * 170.) * lt - 70. * lt * lt);
        float g = 1. - u;
        tint = vec4(g > .5 ? mix(uMid, uHot, g * 2. - 1.) : mix(uCool, uMid, g * 2.), 1.);
        s = (1.2 + r3 * 1.4) * (1. - .5 * u);
        a = smoothstep(0., .05, lt) * (1. - smoothstep(.45, 1., u));
        vRound = 1.;
      } else a = 0.;
    }
    if (a <= .002) { gl_Position = vec4(2., 2., 0., 1.); gl_PointSize = 0.; return; }
    gl_Position = clipAt(pos);
    gl_PointSize = min(uCell * uDpr * s, uMaxPt);
    vUV0 = (base - .5 * uCell) / uRect.zw;
    vUVs = vec2(uCell) / uRect.zw;
    vTint = tint; vMul = mul; vA = a;
  }`;
  const FS_PIX = `#version 300 es
  precision highp float;
  uniform sampler2D uTex;
  in vec2 vUV0, vUVs;
  in vec4 vTint;
  in vec3 vMul;
  in float vA, vRound;
  out vec4 o;
  void main() {
    if (vRound > .5) { float k = pow(max(0., 1. - length(gl_PointCoord - .5) * 2.), 1.5); o = vec4(vTint.rgb, 1.) * k * vA; return; }
    vec4 c = texture(uTex, vUV0 + gl_PointCoord * vUVs);
    if (c.a < .004) discard;
    c.rgb *= vMul;
    c.rgb = mix(c.rgb, vTint.rgb * c.a, vTint.a);
    o = c * vA;
  }`;

  // A soft light (disc) or a thin ring, at a point of the card.
  const VS_GLOW = `${GLSL}
  uniform vec2 uC;
  uniform float uR;
  out vec2 vQ;
  void main() { vQ = vec2(gl_VertexID & 1, gl_VertexID >> 1) * 2. - 1.; gl_Position = clipAt(uC + vQ * uR); }`;
  const FS_GLOW = `#version 300 es
  precision highp float;
  uniform vec4 uCol;
  uniform float uRing, uW;
  uniform vec2 uC;
  uniform float uR;
  in vec2 vQ;
  out vec4 o;
  void main() {
    float d = length(vQ);
    float rr = (d - uRing) * uR / uW;
    float a = uRing > 0. ? exp(-rr * rr) : exp(-d * d * 4.5);
    o = uCol * a * (1. - smoothstep(.92, 1., d));
  }`;

  // Paint `el` (the copy, on the page where the card was) into a 2D canvas: backgrounds, hairline rings,
  // borders, icons, same-site images, the words in text boxes and each word of text where the browser laid it
  // out, in the page's own fonts. Soft shadows, gradients and ::before/::after content are left out. The key's
  // flood of light is left out too: the close draws its own light at the key.
  function paint(el) {
    const r = el.getBoundingClientRect();
    const w = Math.ceil(r.width + 2 * PAD);
    const tall = Math.ceil(r.height + 2 * PAD);
    const dpr = Math.min(GPU.dpr, GPU.maxTex / w, GPU.maxTex / tall);
    // The origin sits on a device pixel, so the texture lines up with the copy it replaces.
    const ox = Math.round(r.left * GPU.dpr) / GPU.dpr - PAD;
    const oy = Math.round(r.top * GPU.dpr) / GPU.dpr - PAD;
    const cv = document.createElement("canvas");
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(tall * dpr);
    const x = cv.getContext("2d");
    x.scale(dpr, dpr);
    x.translate(-ox, -oy);
    const range = document.createRange();
    const clear = (c) => !c || c === "none" || c === "transparent" || c === "rgba(0, 0, 0, 0)" || /\/ 0\)$/.test(c);
    const box = (b, grow, rad) => {
      x.beginPath();
      x.roundRect(b.left - grow, b.top - grow, b.width + 2 * grow, b.height + 2 * grow, Math.max(0, Math.min(rad, b.width / 2, b.height / 2) + grow));
    };
    const font = (cs) => {
      x.font = `${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
      x.letterSpacing = cs.letterSpacing === "normal" ? "0px" : cs.letterSpacing;
      const m = x.measureText("Hg");
      return [m.fontBoundingBoxAscent, m.fontBoundingBoxDescent];
    };
    const words = (n, cs) => {
      const [asc, desc] = font(cs);
      x.fillStyle = cs.color;
      const s = cs.textTransform === "uppercase" ? n.textContent.toUpperCase() : n.textContent;
      const re = /\S+/g;
      for (let word = re.exec(s); word; word = re.exec(s)) {
        range.setStart(n, word.index);
        range.setEnd(n, word.index + word[0].length);
        const rects = range.getClientRects();
        if (rects.length === 1) { const q = rects[0]; x.fillText(word[0], q.left, q.top + (q.height - asc - desc) / 2 + asc); continue; }
        // A word the browser broke over lines (a long path or link): each line's part where it stands.
        let part = "";
        let at = null;
        for (let k = 0; k <= word[0].length; k += 1) {
          let q = null;
          if (k < word[0].length) {
            range.setStart(n, word.index + k);
            range.setEnd(n, word.index + k + 1);
            q = range.getClientRects()[0];
          }
          if (at && (!q || Math.abs(q.top - at.top) > 1)) { x.fillText(part, at.left, at.top + (at.height - asc - desc) / 2 + asc); part = ""; at = null; }
          if (q) { at ||= q; part += s[word.index + k]; }
        }
      }
    };
    // A text box's own words, which are no text nodes: its value, or else its placeholder, one line each. A password
    // field's value, and a credential's shown as text (`data-credential`, a sign-in code), is drawn masked: the copy
    // holds the secret itself (SPEC MSG-15).
    const field = (el, cs, b) => {
      const masked = el.type === "password" || el.hasAttribute("data-credential");
      const value = (masked ? "•".repeat(el.value.length) : el.value) || el.placeholder;
      if (!value) return;
      const [asc] = font(cs);
      x.fillStyle = el.value ? cs.color : getComputedStyle(el, "::placeholder").color;
      const left = b.left + parseFloat(cs.borderLeftWidth) + parseFloat(cs.paddingLeft);
      const top = b.top + parseFloat(cs.borderTopWidth) + parseFloat(cs.paddingTop);
      const line = parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.3;
      value.split("\n").forEach((text, i) => x.fillText(text, left, top + i * line + (line - parseFloat(cs.fontSize)) / 2 + asc * 0.95));
    };
    const shape = (s) => {
      const n = (k) => s[k].baseVal.value;
      const p = new Path2D();
      if (s.localName === "path") return new Path2D(s.getAttribute("d") || "");
      if (s.localName === "circle") p.arc(n("cx"), n("cy"), n("r"), 0, Math.PI * 2);
      else if (s.localName === "ellipse") p.ellipse(n("cx"), n("cy"), n("rx"), n("ry"), 0, 0, Math.PI * 2);
      else if (s.localName === "rect") p.roundRect(n("x"), n("y"), n("width"), n("height"), n("rx"));
      else if (s.localName === "line") { p.moveTo(n("x1"), n("y1")); p.lineTo(n("x2"), n("y2")); }
      else {
        for (let i = 0; i < s.points.numberOfItems; i += 1) { const q = s.points.getItem(i); if (i) p.lineTo(q.x, q.y); else p.moveTo(q.x, q.y); }
        if (s.localName === "polygon") p.closePath();
      }
      return p;
    };
    const icon = (svg, alpha) => {
      const b = svg.getBoundingClientRect();
      for (const s of svg.querySelectorAll("path, circle, ellipse, rect, line, polyline, polygon")) {
        const cs = getComputedStyle(s);
        const m = s.getCTM();
        if (cs.display === "none" || !m) continue;
        x.save();
        x.translate(b.left, b.top);
        x.transform(m.a, m.b, m.c, m.d, m.e, m.f);
        x.globalAlpha = alpha * Number(cs.opacity);
        const p = shape(s);
        if (!clear(cs.fill)) { x.fillStyle = cs.fill; x.fill(p); }
        if (!clear(cs.stroke)) {
          Object.assign(x, { strokeStyle: cs.stroke, lineWidth: parseFloat(cs.strokeWidth) || 1, lineCap: cs.strokeLinecap, lineJoin: cs.strokeLinejoin });
          x.stroke(p);
        }
        x.restore();
      }
    };
    const walk = (el, alpha) => {
      const cs = getComputedStyle(el);
      if (cs.display === "none" || el.classList.contains("press-flood")) return;
      const a = alpha * Number(cs.opacity);
      if (el.localName === "svg") { if (cs.visibility === "visible") icon(el, a); return; }
      const b = el.getBoundingClientRect();
      const rad = parseFloat(cs.borderTopLeftRadius) || 0;
      const seen = cs.visibility === "visible";
      x.globalAlpha = a;
      if (seen) {
        for (const sh of boxShadows(cs.boxShadow)) {
          if (sh.inset || sh.blur > 1 || !sh.spread) continue;
          x.fillStyle = sh.color;
          box(b, sh.spread, rad);
          x.fill();
        }
        if (!clear(cs.backgroundColor)) { x.fillStyle = cs.backgroundColor; box(b, 0, rad); x.fill(); }
        const bw = parseFloat(cs.borderTopWidth) || 0;
        if (bw && cs.borderTopStyle !== "none" && !clear(cs.borderTopColor)) { x.strokeStyle = cs.borderTopColor; x.lineWidth = bw; box(b, -bw / 2, rad); x.stroke(); }
        if (el.localName === "img" && el.complete && el.naturalWidth && new URL(el.src, location.href).origin === location.origin) x.drawImage(el, b.left, b.top, b.width, b.height);
      }
      const clips = cs.overflowX !== "visible" || cs.overflowY !== "visible";
      if (clips) { x.save(); box(b, 0, rad); x.clip(); }
      if (seen && (el.localName === "textarea" || el.localName === "input")) field(el, cs, b);
      // A closed Details shows only its summary; the browser still measures the rest where it would stand.
      const shown = el.localName === "details" && !el.open ? [...el.children].filter((c) => c.localName === "summary") : el.childNodes;
      for (const n of shown) {
        if (n.nodeType === 1) walk(n, a);
        else if (n.nodeType === 3 && seen && n.textContent.trim()) { x.globalAlpha = a; words(n, cs); }
      }
      if (clips) x.restore();
    };
    walk(el, 1);
    return { cv, rect: [ox, oy, w, tall], surf: surface(el), rad: parseFloat(getComputedStyle(el).borderTopLeftRadius) || 0 };
  }

  // The card's own colour, as 0–1 sRGB, so the pixel closes can tell its ink (words, tags, the key) from paper.
  function surface(el) {
    const probe = document.createElement("canvas");
    probe.width = probe.height = 1;
    const pc = probe.getContext("2d", { willReadFrequently: true });
    const own = getComputedStyle(el).backgroundColor;
    pc.fillStyle = /\/ 0\)$|rgba\(0, 0, 0, 0\)|transparent/.test(own) ? getComputedStyle(document.body).backgroundColor : own;
    pc.fillRect(0, 0, 1, 1);
    const px = pc.getImageData(0, 0, 1, 1).data;
    return [px[0] / 255, px[1] / 255, px[2] / 255];
  }

  function boxShadows(v) {
    if (!v || v === "none") return [];
    const parts = [];
    let depth = 0;
    let cur = "";
    for (const ch of v) {
      if (ch === "(") depth += 1;
      if (ch === ")") depth -= 1;
      if (ch === "," && !depth) { parts.push(cur); cur = ""; } else cur += ch;
    }
    parts.push(cur);
    return parts.map((s) => {
      s = s.trim();
      const inset = /\binset\b/.test(s);
      s = s.replace(/\binset\b/, "").trim();
      const m = s.match(/^([a-z-]+\([^)]*\)|#[0-9a-f]+|[a-z]+)\s+(.*)$/i);
      const n = ((m ? m[2] : s).match(/-?[\d.]+px/g) || []).map(parseFloat);
      return { color: m ? m[1] : "black", inset, blur: n[2] || 0, spread: n[3] || 0 };
    });
  }

  // Compile the programs, with the driver's parallel compile where it has one, polled so the page never waits on
  // it; then check them and draw each once with colour writes off, so the first close starts at once.
  function init() {
    // Built by hand: the idle time can come before app.js, with its `h`, has run.
    const cv = document.createElement("canvas");
    cv.className = "close-fx close-gpu";
    cv.setAttribute("aria-hidden", "true");
    const gl = cv.getContext("webgl2", { alpha: true, premultipliedAlpha: true, antialias: false, depth: false, stencil: false, powerPreference: "high-performance" });
    if (!gl) { fail("WebGL2 is not available"); return; }
    GPU.cv = cv;
    GPU.gl = gl;
    GPU.khr = gl.getExtension("KHR_parallel_shader_compile");
    GPU.maxPt = gl.getParameter(gl.ALIASED_POINT_SIZE_RANGE)[1];
    GPU.maxTex = gl.getParameter(gl.MAX_TEXTURE_SIZE);
    if (GPU.maxPt < 32) { fail(`WebGL points are capped at ${GPU.maxPt} px`); return; }
    cv.addEventListener("webglcontextlost", (event) => {
      event.preventDefault();
      for (const j of GPU.jobs) { j.sheet.remove(); gl.deleteTexture(j.tex); }
      GPU.jobs.length = 0;
      cv.remove();
      fail("the WebGL context was lost");
    });
    const build = (vs, fs) => {
      const p = gl.createProgram();
      for (const [type, src] of [[gl.VERTEX_SHADER, vs], [gl.FRAGMENT_SHADER, fs]]) {
        const s = gl.createShader(type);
        gl.shaderSource(s, src);
        gl.compileShader(s);
        gl.attachShader(p, s);
        p[type] = s;
      }
      gl.linkProgram(p);
      return p;
    };
    GPU.P = { card: build(VS_CARD, FS_CARD), ember: build(VS_EMBER, FS_EMBER), pix: build(VS_PIX, FS_PIX), glow: build(VS_GLOW, FS_GLOW) };
    GPU.vao = gl.createVertexArray();
    const settle = () => {
      if (gl.isContextLost()) return;
      if (GPU.khr && !Object.values(GPU.P).every((p) => gl.getProgramParameter(p, GPU.khr.COMPLETION_STATUS_KHR))) { setTimeout(settle, 50); return; }
      for (const [name, p] of Object.entries(GPU.P)) if (!gl.getProgramParameter(p, gl.LINK_STATUS)) {
        fail(`shader ${name}: ${gl.getProgramInfoLog(p)} ${gl.getShaderInfoLog(p[gl.VERTEX_SHADER])} ${gl.getShaderInfoLog(p[gl.FRAGMENT_SHADER])}`);
        return;
      }
      warm();
      GPU.ready = true;
    };
    setTimeout(settle, 50);
  }

  function fail(why) {
    GPU.ready = false;
    GPU.failed = why;
    logError("the GPU close animations", new Error(`${why}; closes play the other animations`));
  }

  function warm() {
    const gl = GPU.gl;
    const tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array(4));
    const j = { rect: [0, 0, 1, 1], O: [0, 0], maxD: 1, t: 0 };
    gl.colorMask(false, false, false, false);
    gl.bindVertexArray(GPU.vao);
    draw(j, "card", { uM: [0, 0, 0, 0], uRad: 1, uFront: 0, uAmp: 0, uFrom: 0, uEdge: 5, uFlame: 10, uFade: 0, ...FIRE }, gl.TRIANGLE_STRIP, 4);
    draw(j, "ember", { uFront: 0, uAmp: 0, uFrom: 0, uSpeed: 1, ...FIRE }, gl.POINTS, 1);
    draw(j, "pix", { uCell: 3, uCols: 1, uMode: 0, uMaxD: 1, uA: [1, 0, 0, 1], uB: [0, 0, 0, 0], uFront: 0, uAmp: 0, uFrom: 0, uTint: [1, 1, 1], uSurf: [1, 1, 1], ...PLUM }, gl.POINTS, 1);
    draw(j, "glow", { uC: [0, 0], uR: 1, uCol: [0, 0, 0, 0], uRing: 0, uW: 1 }, gl.TRIANGLE_STRIP, 4);
    gl.colorMask(true, true, true, true);
    gl.deleteTexture(tex);
  }

  const INTS = new Set(["uFrom", "uCols", "uMode", "uTex"]);
  function uni(prog, name) {
    let m = GPU.U.get(prog);
    if (!m) GPU.U.set(prog, (m = new Map()));
    if (!m.has(name)) m.set(name, GPU.gl.getUniformLocation(prog, name));
    return m.get(name);
  }
  function draw(j, which, u, mode, count) {
    const gl = GPU.gl;
    const prog = GPU.P[which];
    gl.useProgram(prog);
    const all = { uView: GPU.view, uRect: j.rect, uT: j.t, uDpr: GPU.dpr, uMaxPt: GPU.maxPt, uO: j.O, uTex: 0, ...u };
    for (const [k, v] of Object.entries(all)) {
      const loc = uni(prog, k);
      if (loc === null) continue;
      if (INTS.has(k)) gl.uniform1i(loc, v);
      else if (typeof v === "number") gl.uniform1f(loc, v);
      else gl[`uniform${v.length}fv`](loc, v);
    }
    gl.drawArrays(mode, 0, count);
  }

  // Fire colours, hot to cool, and the plum light of the pixel closes, in sRGB.
  const FIRE = { uHot: [1, 0.95, 0.78], uMid: [1, 0.52, 0.1], uCool: [0.62, 0.08, 0.03] };
  const PLUM = { uHot: [0.97, 0.78, 0.98], uMid: [0.72, 0.28, 0.72], uCool: [0.36, 0.06, 0.34] };

  // A close on the GPU: the copy shows at once (its key springs from the press); in the next task the copy is
  // painted and uploaded and `setup(j)` sets the job's `draw(t)`, `end` and `pop` (when the notice's check
  // swells, s); the canvas takes over from the first frame at or after the key's press-in. A paint that fails
  // logs the error and the copy fades instead.
  function play(close, setup) {
    const sheet = closePiece(close, close.from, close.copy);
    const startAt = performance.now() + close.pressIn;
    setTimeout(() => {
      let j;
      if (!GPU.on) {
        GPU.view = [innerWidth, innerHeight];
        GPU.dpr = Math.min(2, window.devicePixelRatio || 1);
      }
      try {
        const shot = paint(close.copy);
        const gl = GPU.gl;
        const tex = gl.createTexture();
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, tex);
        gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, true);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, shot.cv);
        for (const [k, v] of [[gl.TEXTURE_MIN_FILTER, gl.LINEAR], [gl.TEXTURE_MAG_FILTER, gl.LINEAR], [gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE], [gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE]]) gl.texParameteri(gl.TEXTURE_2D, k, v);
        const rect = shot.rect;
        const O = [close.x - rect[0], close.y - rect[1]];
        const maxD = Math.max(...[[0, 0], [rect[2], 0], [0, rect[3]], [rect[2], rect[3]]].map(([cx, cy]) => Math.hypot(cx - O[0], cy - O[1])));
        const surf = shot.surf;
        j = { name: close.name, sheet, tex, rect, O, maxD, surf, rad: shot.rad, dark: 0.2126 * surf[0] + 0.7152 * surf[1] + 0.0722 * surf[2] < 0.5,
          startAt, t: 0, shown: false };
        setup(j);
      } catch (error) {
        logError("painting the card for its close", error);
        closeEnd(sheet.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 300, fill: "both" }), sheet);
        return;
      }
      GPU.jobs.push(j);
      if (GPU.on) return;
      GPU.on = true;
      GPU.cv.width = Math.round(innerWidth * GPU.dpr);
      GPU.cv.height = Math.round(innerHeight * GPU.dpr);
      Object.assign(GPU.cv.style, { left: "0", top: "0", width: `${innerWidth}px`, height: `${innerHeight}px` });
      GPU.cv.dataset.close = j.name;
      document.body.append(GPU.cv);
      requestAnimationFrame(frame);
    }, 0);
  }

  // Each close runs on the clock from its start, so slow frames or a page in the background never stretch it.
  function frame(now) {
    const gl = GPU.gl;
    gl.viewport(0, 0, GPU.cv.width, GPU.cv.height);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.bindVertexArray(GPU.vao);
    gl.activeTexture(gl.TEXTURE0);
    let live = 0;
    for (const j of GPU.jobs) {
      j.t = (now - j.startAt) / 1000;
      if (j.t >= 0 && j.t < j.end) {
        if (!j.shown) {
          j.shown = true;
          j.sheet.remove();
          noticePop(noticeMark(), Math.max(0, j.pop - j.t) * 1000);
        }
        gl.bindTexture(gl.TEXTURE_2D, j.tex);
        j.draw(j.t);
      }
      if (j.t < j.end) GPU.jobs[live++] = j;
      else { j.sheet.remove(); gl.deleteTexture(j.tex); }
    }
    GPU.jobs.length = live;
    if (live) { requestAnimationFrame(frame); return; }
    GPU.on = false;
    gl.clear(gl.COLOR_BUFFER_BIT);
    GPU.cv.remove();
  }

  const strip = () => GPU.gl.TRIANGLE_STRIP;

  // Burn and Ignite: a ragged glowing front, from the key (`from` 0) or up from the bottom edge (2), runs over the
  // card in `time` ms; the paper browns and shimmers just ahead of it and chars behind it, with flame tongues `flame`
  // px tall and embers rising off it. Both run at the fast end of the lab's range
  function burn(close, { from, time, flame, edge, ragged, embers }) {
    play(close, (j) => {
      const D = time / 1000;
      const reach = from === 0 ? j.maxD : j.rect[3];
      const F = reach + ragged + edge * 3 + flame + 12;
      const m = from === 2 ? [12, flame + 30, 12, 4] : [flame + 30, flame + 30, flame + 30, flame + 30];
      j.end = D + 1.25;
      j.pop = D * 0.8;
      j.draw = (t) => {
        const front = (F * Math.min(t, D)) / D;
        if (t < D + 0.05) draw(j, "card", { uM: m, uRad: j.rad, uFront: front, uAmp: ragged, uFrom: from, uEdge: edge, uFlame: flame, uFade: 1, ...FIRE }, strip(), 4);
        draw(j, "ember", { uFront: front, uAmp: ragged, uFrom: from, uSpeed: F / D, ...FIRE }, GPU.gl.POINTS, embers);
        if (from === 0) draw(j, "glow", { uC: j.O, uR: 70, uRing: 0, uCol: [...FIRE.uHot.map((c) => c * 0.9 * Math.exp(-t * 7)), 0.9 * Math.exp(-t * 7)] }, strip(), 4);
      };
    });
  }

  // The card as its own pixels: one GPU point per cell of `grain` px, each drawing its patch of the texture.
  // At most about 90,000 points: a big card gets coarser cells.
  function pixels(j, grain, mode) {
    const g = Math.max(grain, Math.sqrt((j.rect[2] * j.rect[3]) / 90000));
    const cell = Math.max(1, Math.round(g * GPU.dpr)) / GPU.dpr;
    const cols = Math.ceil(j.rect[2] / cell);
    return { n: cols * Math.ceil(j.rect[3] / cell), u: { uCell: cell, uCols: cols, uMode: mode, uMaxD: j.maxD, uB: [0, 0, 0, 0], uFront: 0, uAmp: 0, uFrom: 0, uTint: [1, 1, 1], uSurf: j.surf, ...PLUM } };
  }

  // Snap: from the key outwards, every patch of the card lets go in a ragged sweep over 460 ms and drifts off
  // on a swirling wind, greying as it goes.
  function closeSnap(close) {
    play(close, (j) => {
      const D = 0.46;
      const wind = 220;
      const swirl = 1;
      const linger = 0.9;
      const { n, u } = pixels(j, 2.5, 0);
      Object.assign(u, { uA: [D, wind, swirl, linger], uTint: j.dark ? [0.62, 0.6, 0.58] : [0.56, 0.53, 0.5] });
      j.end = D + linger + 0.1;
      j.pop = D * 0.8;
      j.draw = (t) => {
        draw(j, "pix", u, GPU.gl.POINTS, n);
        const g = 0.55 * Math.exp(-t * 9);
        if (g > 0.01) draw(j, "glow", { uC: j.O, uR: 46, uRing: 0, uCol: [0.75 * g, 0.5 * g, 0.78 * g, g] }, strip(), 4);
      };
    });
  }

  // Vortex: a plum eddy opens at the key and the card's pixels spiral into it, nearest first, each falling in
  // over 380 ms and the farthest starting 240 ms late; a last flash, and it closes.
  function closeVortex(close) {
    play(close, (j) => {
      const pull = 0.38;
      const lag = 0.24;
      const glow = 0.8;
      const T = lag + pull * 1.2 + 0.08;
      const { n, u } = pixels(j, 3, 1);
      u.uA = [pull, lag, 7, 0];
      j.end = T + 0.18;
      j.pop = T;
      j.draw = (t) => {
        const shut = Math.max(0, (t - T) / 0.18);
        const g = glow * Math.min(1, t / 0.12) * (1 - shut);
        draw(j, "glow", { uC: j.O, uR: 60 + 30 * Math.min(1, t / T), uRing: 0, uCol: [0.55 * g, 0.16 * g, 0.55 * g, 0.75 * g] }, strip(), 4);
        draw(j, "pix", u, GPU.gl.POINTS, n);
        if (shut > 0) { const f = glow * (1 - shut); draw(j, "glow", { uC: j.O, uR: 26 + 50 * shut, uRing: 0.7, uW: 3, uCol: [f, 0.8 * f, f, f] }, strip(), 4); }
      };
    });
  }

  // Blast: a shock ring leaves the key at 1,700 px/s, faster on a card too tall to cross in 0.6 s; as it passes,
  // it ripples the card and then knocks its pixels outward and towards you in perspective, white-hot for an
  // instant, falling and fading.
  function closeBlast(close) {
    play(close, (j) => {
      const wave = Math.max(1700, j.maxD / 0.6);
      const flash = 0.7;
      const W = j.maxD / wave;
      const { n, u } = pixels(j, 3, 2);
      u.uA = [wave, 1, 1, 0];
      j.end = W + 0.9;
      j.pop = W;
      j.draw = (t) => {
        draw(j, "pix", u, GPU.gl.POINTS, n);
        const f = flash * Math.exp(-t * 9);
        if (f > 0.01) draw(j, "glow", { uC: j.O, uR: 90, uRing: 0, uCol: [f, 0.9 * f, f, f] }, strip(), 4);
        const ra = flash * Math.max(0, 1 - t / (W + 0.15));
        if (ra > 0.01) {
          // The shock front: a thin plum line with a faint wake, thinning as it spreads.
          const R = wave * t;
          const reach = R + 40;
          draw(j, "glow", { uC: j.O, uR: reach, uRing: R / reach, uW: 16, uCol: [0.075 * ra, 0.025 * ra, 0.08 * ra, 0.25 * ra] }, strip(), 4);
          draw(j, "glow", { uC: j.O, uR: reach, uRing: R / reach, uW: 2.2, uCol: [0.52 * ra, 0.2 * ra, 0.55 * ra, 0.75 * ra] }, strip(), 4);
        }
      };
    });
  }

  (window.requestIdleCallback || ((f) => setTimeout(f, 500)))(init, { timeout: 3000 });

  return {
    ready: () => GPU.ready,
    burn: (close) => burn(close, { from: 0, time: 300, flame: 14, edge: 6, ragged: 120, embers: 380 }),
    ignite: (close) => burn(close, { from: 2, time: 350, flame: 70, edge: 5, ragged: 55, embers: 300 }),
    snap: closeSnap,
    vortex: closeVortex,
    blast: closeBlast,
  };
})();
