/* ============================================================================
 * NetWorth - Shareable Player Card (customizer + export + share)
 *
 * Loaded AFTER app.js, reusing its globals (API_BASE_URL, allPlayers,
 * myPlayerId, getAuthHeaders, authedFetch, loadPlayers, loadStoreCatalogOnce,
 * imageSrc). Classic scripts share one global lexical scope.
 *
 * Three customizable axes: BACKGROUND, FRAME, STATS layout. Each is
 * owned/locked through the existing store (owned_items + /store-purchase);
 * equipping persists via /update-my-card. Locked picks render dulled with a
 * baked watermark, and the export refuses to bake a locked pick.
 *
 * Cosmetic families through one store:
 *   - image cosmetics  : uploaded PNGs (background_image, card_frame)
 *   - preset cosmetics : code-defined visuals named by effect.value
 *       background_preset -> premium backgrounds (incl. animated)
 *       card_frame_preset -> premium frames (incl. glass)
 *       card_layout       -> premium stats layouts (curve/donut)
 * Preset visuals live here so they render in the live preview (css) AND the
 * exported PNG (canvas). Animated presets animate in the preview only; the
 * still export bakes a representative frame.
 * ========================================================================== */
(function () {
  'use strict';

  // ---- helpers -------------------------------------------------------------
  function api() { return (typeof API_BASE_URL !== 'undefined') ? API_BASE_URL : ''; }
  function authHeaders() { return (typeof getAuthHeaders === 'function') ? getAuthHeaders() : {}; }
  function meId() { return (typeof myPlayerId === 'function') ? myPlayerId() : null; }
  function players() { return (typeof allPlayers !== 'undefined' && Array.isArray(allPlayers)) ? allPlayers : []; }
  function srcOf(key) { return (typeof imageSrc === 'function') ? imageSrc(key) : (key ? '/' + key : null); }
  function myPlayer() { const id = meId(); return players().find(p => p.player_id === id) || null; }
  function levelFromXp(xp) { return Math.max(1, Math.floor(Math.sqrt(Math.floor((Number(xp) || 0) / 5)))); }
  function rankOf(p) {
    if (!p) return null;
    const sorted = players().filter(x => x.claimed).sort((a, b) => Number(b.rating) - Number(a.rating));
    const i = sorted.findIndex(x => x.player_id === p.player_id);
    return i >= 0 ? i + 1 : null;
  }
  function initials(name) {
    return (name || '?').trim().split(/\s+/).slice(0, 2).map(w => w[0] || '').join('').toUpperCase() || '?';
  }

  // ---- preset visuals ------------------------------------------------------
  function bgCss(a, b, c) {
    return `linear-gradient(rgba(7,12,10,.28),rgba(7,12,10,.5)),linear-gradient(160deg,${a} 0%,${b} 55%,${c} 100%)`;
  }
  function animBgCss(a, b, c) {
    return `linear-gradient(rgba(7,12,10,.26),rgba(7,12,10,.48)),linear-gradient(120deg,${a} 0%,${b} 38%,${c} 68%,${a} 100%)`;
  }

  // Free backgrounds map to background_id (must be in the backend allow-list).
  const FREE_BG = [
    { id: 'court', name: 'Court', css: bgCss('#0b3018', '#12452a', '#1F7A4D') },
    { id: 'plain', name: 'Plain', css: bgCss('#0c110e', '#121814', '#1a221d') }
  ];

  // Premium backgrounds -> background_preset store items (ownership-gated).
  // canvas = [top,mid,bottom] stops for the export gradient. anim = css class
  // used only in the live preview.
  const PREMIUM_BG = [
    { id: 'nebula',    name: 'Nebula',        css: bgCss('#0a0a1f', '#141433', '#2a1a4d'), canvas: ['#0a0a1f', '#141433', '#2a1a4d'] },
    { id: 'ember',     name: 'Ember',         css: bgCss('#14090a', '#2b1008', '#7a2410'), canvas: ['#14090a', '#2b1008', '#7a2410'] },
    { id: 'blueprint', name: 'Blueprint',     css: bgCss('#041d2e', '#053046', '#063b5c'), canvas: ['#041d2e', '#053046', '#063b5c'] },
    { id: 'aurora',    name: 'Aurora',        css: bgCss('#04121a', '#0a2e3a', '#116b5a'), canvas: ['#04121a', '#0a2e3a', '#116b5a'] },
    { id: 'circuit',   name: 'Circuit',       css: bgCss('#050912', '#0a1430', '#12306b'), canvas: ['#050912', '#0a1430', '#12306b'] },
    { id: 'flame',     name: 'Flame',         css: bgCss('#160604', '#3a0f08', '#7a1e0a'), canvas: ['#160604', '#3a0f08', '#7a1e0a'] },
    { id: 'galaxy',    name: 'Galaxy',        css: bgCss('#0a0518', '#1a0a33', '#3a1a5c'), canvas: ['#0a0518', '#1a0a33', '#3a1a5c'] },
    { id: 'ocean',     name: 'Ocean',         css: bgCss('#04141a', '#083245', '#0d5a7a'), canvas: ['#04141a', '#083245', '#0d5a7a'] },
    { id: 'sunset',    name: 'Sunset',        css: bgCss('#1a0812', '#3a1020', '#7a2a1a'), canvas: ['#1a0812', '#3a1020', '#7a2a1a'] },
    { id: 'drift',     name: 'Aurora Drift *', css: animBgCss('#04121a', '#0a2e3a', '#116b5a'), anim: 'nw-cs-bg-drift', canvas: ['#04121a', '#0a2e3a', '#116b5a'] },
    { id: 'pulse',     name: 'Nebula Pulse *', css: animBgCss('#0a0518', '#1a0a33', '#3a1a5c'), anim: 'nw-cs-bg-pulse', canvas: ['#0a0518', '#1a0a33', '#3a1a5c'] }
  ];
  const PREMIUM_BG_BY_ID = Object.fromEntries(PREMIUM_BG.map(b => [b.id, b]));

  const FRAME_PRESETS = [
    { id: 'gold',   name: 'Gold Elite' },
    { id: 'holo',   name: 'Holo (glass)' },
    { id: 'ice',    name: 'Ice (glass)' },
    { id: 'plasma', name: 'Plasma (glass)' },
    { id: 'flame',  name: 'Flame' },
    { id: 'ruby',   name: 'Ruby' },
    { id: 'chrome', name: 'Chrome' },
    { id: 'carbon', name: 'Carbon' },
    { id: 'neon',   name: 'Neon' }
  ];
  const FRAME_NAME = Object.fromEntries(FRAME_PRESETS.map(f => [f.id, f.name]));

  const STATS_PRESETS = [
    { id: 'full',    name: 'Full profile', free: true },
    { id: 'compact', name: 'Compact',      free: true },
    { id: 'peak',    name: 'Peak rating',  free: true },
    { id: 'streak',  name: 'Streaks',      free: true },
    { id: 'record',  name: 'Win record',   free: true },
    { id: 'form',    name: 'Last 10',      free: true },
    { id: 'stuffed', name: 'Everything',   free: true },
    { id: 'vs',      name: 'Head-to-head', free: true },
    { id: 'partner', name: 'Best partner', free: true },
    { id: 'curve',   name: 'Rating curve', free: false },
    { id: 'donut',   name: 'Season donut', free: false }
  ];
  const STATS_NAME = Object.fromEntries(STATS_PRESETS.map(s => [s.id, s.name]));

  // Exposed for app.js (store form dropdown + store list preview swatches).
  window.NW_CARD_PRESETS = {
    kinds: {
      card_frame_preset: FRAME_PRESETS.map(f => ({ id: f.id, name: f.name })),
      background_preset:  PREMIUM_BG.map(b => ({ id: b.id, name: b.name })),
      card_layout:        STATS_PRESETS.filter(s => !s.free).map(s => ({ id: s.id, name: s.name }))
    },
    // A small visual preview for the store lists. extraCls scales it up.
    swatchHtml: function (kind, value, extraCls) {
      const cls = 'nw-cs-sw' + (extraCls ? ' ' + extraCls : '');
      if (kind === 'background_preset') {
        const b = PREMIUM_BG_BY_ID[value];
        const css = b ? b.css : bgCss('#101511', '#1a221d', '#243029');
        const anim = (b && b.anim) ? ' ' + b.anim : '';
        return `<span class="${cls}${anim}" style="background:${css};"></span>`;
      }
      if (kind === 'card_frame_preset') {
        return `<span class="${cls} nw-cs-fr-${value}" style="padding:3px;"><span style="display:block;width:100%;height:100%;border-radius:4px;background:#0e1a14;"></span></span>`;
      }
      if (kind === 'card_layout') {
        return `<span class="${cls}" style="background:#0e1a14;display:flex;align-items:center;justify-content:center;font:700 8px Inter;letter-spacing:.5px;color:#7fd8a8;">${(STATS_NAME[value] || value).toUpperCase()}</span>`;
      }
      return '';
    }
  };

  // ---- canvas painters -----------------------------------------------------
  function rr(ctx, x, y, w, h, r) {
    ctx.beginPath(); ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
  }
  function glassStreak(ctx, W, H, rad, t) {
    const p = (t == null) ? 0.42 : t;               // sweep position 0..1
    const cx = (-0.3 + 1.6 * p) * W, band = W * 0.16, sk = H * 0.14;
    ctx.save(); rr(ctx, 4, 4, W - 8, H - 8, rad + 8); ctx.clip();
    const gr = ctx.createLinearGradient(cx - band, 0, cx + band, 0);
    gr.addColorStop(0, 'rgba(255,255,255,0)'); gr.addColorStop(.5, 'rgba(255,255,255,.24)'); gr.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.fillStyle = gr; ctx.beginPath();
    ctx.moveTo(cx - band + sk, 0); ctx.lineTo(cx + band + sk, 0); ctx.lineTo(cx + band - sk, H); ctx.lineTo(cx - band - sk, H); ctx.closePath(); ctx.fill();
    ctx.restore();
  }
  function gradStroke(ctx, W, H, rad, stops, lw) {
    const g = ctx.createLinearGradient(0, 0, W, H);
    stops.forEach(s => g.addColorStop(s[0], s[1]));
    ctx.strokeStyle = g; ctx.lineWidth = lw; rr(ctx, 14, 14, W - 28, H - 28, rad); ctx.stroke();
  }
  const GLASS_STOPS = {
    holo:   [[0, '#00e0ff'], [.35, '#ff4dd2'], [.7, '#ffd24d'], [1, '#00e0ff']],   // cyan-magenta-gold rainbow
    ice:    [[0, '#dff9ff'], [.4, '#7fd8ff'], [.7, '#bff0ff'], [1, '#5ab8e6']],     // pale icy blue/white
    plasma: [[0, '#7a2cff'], [.4, '#b026ff'], [.7, '#5a3cff'], [1, '#8a2cff']]      // deep violet/purple
  };
  function flameTongue(ctx, cx, baseY, w, h) {   // grows +y (inward) by h in local space
    ctx.beginPath(); ctx.moveTo(cx - w / 2, baseY);
    ctx.quadraticCurveTo(cx - w * 0.1, baseY + h * 0.5, cx, baseY + h);
    ctx.quadraticCurveTo(cx + w * 0.1, baseY + h * 0.5, cx + w / 2, baseY);
    ctx.closePath();
  }
  function flameEdge(ctx, along, t) {            // local: x 0..along, +y points inward
    const n = Math.max(5, Math.round(along / 46)), base = 16;
    for (let i = 0; i <= n; i++) {
      const cx = (i / n) * along, w = (along / n) * 1.2;
      const h = 40 + Math.sin(t * 6.283 + i * 1.3) * 14 + (i % 2 ? 8 : 0);
      let g = ctx.createLinearGradient(0, base, 0, base + h);
      g.addColorStop(0, '#ff7a12'); g.addColorStop(.5, '#ff3d0a'); g.addColorStop(1, 'rgba(200,24,0,0)');
      ctx.fillStyle = g; flameTongue(ctx, cx, base, w, h); ctx.fill();
      g = ctx.createLinearGradient(0, base, 0, base + h * 0.6);
      g.addColorStop(0, '#ffe694'); g.addColorStop(1, 'rgba(255,150,20,0)');
      ctx.fillStyle = g; flameTongue(ctx, cx, base, w * 0.48, h * 0.6); ctx.fill();
    }
  }
  function drawFlame(ctx, W, H, t) {
    const rad = 44, tt = (t == null ? 0.3 : t);
    ctx.strokeStyle = '#5a1508'; ctx.lineWidth = 8; rr(ctx, 12, 12, W - 24, H - 24, rad); ctx.stroke();
    ctx.save(); rr(ctx, 10, 10, W - 20, H - 20, rad + 4); ctx.clip();
    ctx.save(); flameEdge(ctx, W, tt); ctx.restore();                                  // top
    ctx.save(); ctx.translate(W, 0); ctx.rotate(Math.PI / 2); flameEdge(ctx, H, tt + 0.25); ctx.restore();   // right
    ctx.save(); ctx.translate(W, H); ctx.rotate(Math.PI); flameEdge(ctx, W, tt + 0.5); ctx.restore();        // bottom
    ctx.save(); ctx.translate(0, H); ctx.rotate(-Math.PI / 2); flameEdge(ctx, H, tt + 0.75); ctx.restore();  // left
    ctx.restore();
  }
  function drawFramePreset(ctx, id, W, H, t) {
    const rad = 44;
    ctx.save();
    if (id === 'gold') {
      gradStroke(ctx, W, H, rad, [[0, '#f9df8a'], [.35, '#b9871f'], [.55, '#ffe9a8'], [.75, '#a06b12'], [1, '#f7d774']], 10);
      ctx.strokeStyle = 'rgba(255,240,190,.35)'; ctx.lineWidth = 2; rr(ctx, 22, 22, W - 44, H - 44, rad - 6); ctx.stroke();
    } else if (id === 'ruby') {
      gradStroke(ctx, W, H, rad, [[0, '#ff8ea6'], [.4, '#c11f45'], [.7, '#ff5a7d'], [1, '#8a1230']], 10);
    } else if (id === 'flame') {
      drawFlame(ctx, W, H, t);
    } else if (id === 'chrome') {
      gradStroke(ctx, W, H, rad, [[0, '#f2f5f8'], [.35, '#9aa6b2'], [.55, '#ffffff'], [.75, '#7d8794'], [1, '#dfe6ec']], 10);
    } else if (id === 'carbon') {
      ctx.strokeStyle = '#2a302c'; ctx.lineWidth = 12; rr(ctx, 14, 14, W - 28, H - 28, rad); ctx.stroke();
      ctx.strokeStyle = 'rgba(127,216,168,.25)'; ctx.lineWidth = 2; rr(ctx, 21, 21, W - 42, H - 42, rad - 5); ctx.stroke();
    } else if (id === 'neon') {
      ctx.shadowColor = '#33f0c0'; ctx.shadowBlur = 26; ctx.strokeStyle = '#5affd0'; ctx.lineWidth = 6;
      rr(ctx, 14, 14, W - 28, H - 28, rad); ctx.stroke(); ctx.shadowBlur = 0;
    } else if (GLASS_STOPS[id]) {
      gradStroke(ctx, W, H, rad, GLASS_STOPS[id], 9);
      glassStreak(ctx, W, H, rad, t);
    }
    ctx.restore();
  }
  function drawDonut(ctx, cx, cy, r, pct) {
    ctx.save(); ctx.lineWidth = r * 0.34;
    ctx.strokeStyle = '#26332c'; ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.stroke();
    ctx.lineCap = 'round'; ctx.strokeStyle = '#7fd8a8';
    ctx.beginPath(); ctx.arc(cx, cy, r, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * (Math.max(0, Math.min(100, pct)) / 100)); ctx.stroke();
    ctx.restore();
  }
  function drawSpark(ctx, x, y, w, h, pts) {
    if (!pts || pts.length < 2) return;
    const mn = Math.min(...pts) - 4, mx = Math.max(...pts) + 4, span = Math.max(1, mx - mn);
    const P = pts.map((v, i) => [x + i * (w / (pts.length - 1)), y + h - ((v - mn) / span) * h]);
    ctx.beginPath(); ctx.moveTo(P[0][0], y + h); P.forEach(p => ctx.lineTo(p[0], p[1])); ctx.lineTo(P[P.length - 1][0], y + h); ctx.closePath();
    ctx.fillStyle = 'rgba(127,216,168,.15)'; ctx.fill();
    ctx.beginPath(); P.forEach((p, i) => i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]));
    ctx.strokeStyle = '#7fd8a8'; ctx.lineWidth = 4; ctx.lineJoin = 'round'; ctx.stroke();
    const pk = P[P.length - 1]; ctx.beginPath(); ctx.arc(pk[0], pk[1], 7, 0, Math.PI * 2);
    ctx.fillStyle = '#eafff3'; ctx.fill(); ctx.strokeStyle = '#7fd8a8'; ctx.lineWidth = 3; ctx.stroke();
  }

  // ---- state ---------------------------------------------------------------
  let modal = null, stats = null;
  let bgOpts = [], frameOpts = [], statsOpts = [];
  let cat = 0;
  const idx = [0, 0, 0];
  const catNames = ['Background', 'Frame', 'Stats'];
  function listFor(c) { return [bgOpts, frameOpts, statsOpts][c]; }
  const wrap = (n, l) => l ? ((n % l) + l) % l : 0;

  // ---- styles --------------------------------------------------------------
  function injectStyles() {
    if (document.getElementById('nw-cardshare-styles')) return;
    const s = document.createElement('style');
    s.id = 'nw-cardshare-styles';
    s.textContent = `
    #nw-cs-modal{position:fixed; inset:0; z-index:1200; background:rgba(4,7,6,.82); display:none; overflow-y:auto;}
    #nw-cs-modal.open{display:block;}
    .nw-cs-wrap{max-width:520px; margin:0 auto; padding:18px 12px 40px; font-family:Inter,system-ui,sans-serif; color:#e8efe9;}
    .nw-cs-top{display:flex; align-items:center; justify-content:space-between; margin-bottom:6px;}
    .nw-cs-top h2{font:700 17px Inter; margin:0;}
    .nw-cs-coin{font:700 13px Inter; background:#141a17; border:1px solid #26332b; color:#f7d774; padding:6px 12px; border-radius:999px;}
    .nw-cs-x{background:none; border:none; color:#cfe1d8; font-size:22px; cursor:pointer; margin-left:8px; line-height:1;}
    .nw-cs-sub{color:#8fa39a; font-size:12px; line-height:1.5; margin:4px 0 12px;}
    .nw-cs-cats{display:flex; justify-content:center; gap:8px; margin:4px 0;}
    .nw-cs-cat{font:600 11px Inter; letter-spacing:.3px; color:#7c8f87; background:#121815; border:1px solid #202b25; padding:7px 15px; border-radius:999px; cursor:pointer;}
    .nw-cs-cat.on{color:#eafff3; border-color:#2f7a52; background:#173a29;}
    .nw-cs-stage{position:relative; height:462px; margin-top:6px; touch-action:pan-y;}
    .nw-cs-slot{position:absolute; top:50%; left:50%; transition:transform .32s cubic-bezier(.25,.8,.32,1), opacity .28s ease;}
    .nw-cs-noanim .nw-cs-slot{transition:none !important;}
    #nw-cs-cur{z-index:5; transform:translate(-50%,-50%) scale(1.04);}
    #nw-cs-prev{z-index:2; transform:translate(calc(-50% - 250px),-50%) scale(.58); opacity:.12;}
    #nw-cs-next{z-index:2; transform:translate(calc(-50% + 250px),-50%) scale(.58); opacity:.12;}
    .nw-cs-chev{position:absolute; top:50%; transform:translateY(-50%); z-index:8; font:300 42px Inter; color:#cfe8db; cursor:pointer; width:42px; text-align:center; opacity:.4; user-select:none;}
    .nw-cs-chev:hover{opacity:.9;} .nw-cs-chev.l{left:0;} .nw-cs-chev.r{right:0;}
    .nw-cs-ud{position:absolute; left:50%; transform:translateX(-50%); z-index:9; background:#141a17cc; border:1px solid #253128; color:#cfe1d8; width:36px; height:26px; border-radius:8px; cursor:pointer; font-size:13px;}
    .nw-cs-ud.up{top:-2px;} .nw-cs-ud.down{bottom:-2px;}
    .nw-cs-frame{width:296px; border-radius:26px; padding:3px; position:relative;}
    .nw-cs-card{border-radius:23px; position:relative; overflow:hidden; padding:20px 20px 16px; min-height:406px; display:flex; flex-direction:column;}
    .nw-cs-card::after{content:''; position:absolute; inset:0; border-radius:23px; box-shadow:inset 0 0 60px rgba(0,0,0,.28); pointer-events:none;}
    .nw-cs-content{position:relative; z-index:1; display:flex; flex-direction:column; flex:1; text-shadow:0 1px 3px rgba(0,0,0,.55);}
    .nw-cs-content.dim{filter:grayscale(.85) brightness(.62);}
    .nw-cs-frimg{position:absolute; inset:0; z-index:3; background-size:100% 100%; background-repeat:no-repeat; pointer-events:none; border-radius:23px;}
    .nw-cs-min{padding:1.5px; background:rgba(127,216,168,.4);}
    .nw-cs-fr-gold{padding:3px; background:linear-gradient(135deg,#f9df8a,#b9871f 35%,#ffe9a8 55%,#a06b12 75%,#f7d774);}
    .nw-cs-fr-ruby{padding:3px; background:linear-gradient(135deg,#ff8ea6,#c11f45 40%,#ff5a7d 70%,#8a1230);}
    .nw-cs-fr-flame{padding:3px; background:linear-gradient(135deg,#8a2410,#3a0f06 50%,#6e1c0a); box-shadow:0 0 16px rgba(255,90,20,.35);}
    .nw-cs-fr-flame .nw-cs-content{padding:38px 30px 26px;}
    .nw-cs-flames{position:absolute; inset:0; z-index:2; pointer-events:none; filter:drop-shadow(0 0 5px rgba(255,120,20,.5)); animation:nw-cs-flick .9s ease-in-out infinite alternate;}
    @keyframes nw-cs-flick{from{opacity:.82; transform:scale(1);}to{opacity:1; transform:scale(1.015);}}
    .nw-cs-fr-chrome{padding:3px; background:linear-gradient(135deg,#f2f5f8,#9aa6b2 35%,#fff 55%,#7d8794 75%,#dfe6ec);}
    .nw-cs-fr-carbon{padding:3px; background:repeating-linear-gradient(45deg,#3a423d 0 3px,#1c211e 3px 6px);}
    .nw-cs-fr-neon{padding:3px; background:#5affd0; box-shadow:0 0 18px rgba(51,240,192,.55);}
    .nw-cs-fr-holo{padding:3px; background:linear-gradient(115deg,#00e0ff,#ff4dd2,#ffd24d,#00e0ff); background-size:300% 300%; animation:nw-cs-holo 4.5s linear infinite;}
    .nw-cs-fr-ice{padding:3px; background:linear-gradient(115deg,#dff9ff,#7fd8ff,#bff0ff,#5ab8e6); background-size:300% 300%; animation:nw-cs-holo 5s linear infinite;}
    .nw-cs-fr-plasma{padding:3px; background:linear-gradient(115deg,#7a2cff,#b026ff,#5a3cff,#8a2cff); background-size:300% 300%; animation:nw-cs-holo 4s linear infinite;}
    @keyframes nw-cs-holo{0%{background-position:0% 50%;}100%{background-position:300% 50%;}}
    .nw-cs-fr-holo .nw-cs-card::before,.nw-cs-fr-ice .nw-cs-card::before,.nw-cs-fr-plasma .nw-cs-card::before{content:''; position:absolute; inset:0; z-index:5; border-radius:23px; pointer-events:none;
      background:linear-gradient(115deg,transparent 35%,rgba(255,255,255,.22) 50%,transparent 65%); background-size:250% 100%; animation:nw-cs-glass 3s linear infinite;}
    @keyframes nw-cs-glass{0%{background-position:120% 0;}100%{background-position:-120% 0;}}
    .nw-cs-bg-drift{background-size:230% 230% !important; animation:nw-cs-drift 9s ease-in-out infinite;}
    @keyframes nw-cs-drift{0%{background-position:0% 50%;}50%{background-position:100% 50%;}100%{background-position:0% 50%;}}
    .nw-cs-bg-pulse{background-size:170% 170% !important; animation:nw-cs-pulse 4.5s ease-in-out infinite;}
    @keyframes nw-cs-pulse{0%,100%{background-position:38% 38%;}50%{background-position:62% 62%;}}
    .nw-cs-veil{position:absolute; inset:0; z-index:7; border-radius:23px; display:flex; flex-direction:column; align-items:center; justify-content:center; background:rgba(6,10,8,.5);}
    .nw-cs-veil .wm{position:absolute; font:800 30px Rajdhani,Inter; letter-spacing:6px; color:rgba(255,255,255,.06); transform:rotate(-24deg);}
    .nw-cs-veil .lk{font:800 15px Rajdhani,Inter; letter-spacing:3px; color:#e9f3ee;}
    .nw-cs-veil .pc{font:700 13px Inter; color:#f7d774; margin-top:3px;}
    .nw-cs-unlock{margin-top:11px; font:600 12px Inter; background:#1f7a4d; color:#eafff3; border:none; padding:9px 16px; border-radius:9px; cursor:pointer;}
    .nw-cs-cap{font:600 9.5px Inter; letter-spacing:1.4px; color:#93a89e;}
    .nw-cs-rating{font:800 52px Rajdhani,Inter; color:#7fd8a8; line-height:.82; letter-spacing:-1px;}
    .nw-cs-av{width:78px; height:78px; border-radius:50%; border:2px solid rgba(127,216,168,.55); background:#0e1a14 center/cover no-repeat; display:flex; align-items:center; justify-content:center; font:800 26px Rajdhani,Inter; color:#7fd8a8; flex:none;}
    .nw-cs-rt{display:flex; justify-content:space-between; align-items:flex-start;}
    .nw-cs-lr{font:400 12.5px Inter; color:#c9d6cf; margin-top:5px;} .nw-cs-lr b{font-weight:700; color:#fff;} .nw-cs-lr .m{color:#8fa39a;}
    .nw-cs-name{font:700 22px Inter; text-align:center; margin-top:14px;}
    .nw-cs-handle{font:500 13px Inter; text-align:center; color:#8fa39a; margin-top:1px;}
    .nw-cs-div{height:1px; background:rgba(127,216,168,.22); margin:13px 0;}
    .nw-cs-grid{display:grid; grid-template-columns:1fr 1fr; gap:13px 10px;}
    .nw-cs-grid .v{font:700 20px Rajdhani,Inter; color:#fff;}
    .nw-cs-trend{display:flex; align-items:flex-end; gap:6px; height:52px; margin-bottom:6px;}
    .nw-cs-trend i{flex:1; border-radius:4px 4px 2px 2px; display:block;}
    .nw-cs-foot{margin-top:auto; padding-top:12px; text-align:center; font:700 11px Rajdhani,Inter; letter-spacing:3px; color:#5bbf8a;}
    .nw-cs-foot .d{color:#f7d774; margin:0 6px;}
    .nw-cs-big{font:800 66px Rajdhani,Inter; color:#7fd8a8; text-align:center; line-height:.85; letter-spacing:-2px;}
    .nw-cs-rec{text-align:center; font:600 15px Inter; color:#dfece5; margin-top:8px;}
    .nw-cs-strk{text-align:center; font:500 13px Inter; color:#9fb3a8; margin-top:3px;}
    .nw-cs-spark{width:100%; height:88px; margin:4px 0 2px;}
    .nw-cs-optname{text-align:center; font:600 13px Inter; color:#cfe1d8; margin-top:8px; min-height:18px;}
    .nw-cs-dots{display:flex; justify-content:center; gap:6px; margin-top:6px; flex-wrap:wrap;}
    .nw-cs-dots span{width:6px; height:6px; border-radius:50%; background:#2b3831;}
    .nw-cs-dots span.on{background:#7fd8a8; width:16px; border-radius:3px;}
    .nw-cs-dots span.lock{background:#5a4a22;}
    .nw-cs-hint{text-align:center; color:#5f726a; font-size:11px; margin-top:8px; min-height:15px;}
    .nw-cs-hint b{color:#c98;}
    .nw-cs-actions{display:flex; gap:10px; margin-top:16px;}
    .nw-cs-actions button{flex:1; font:600 14px Inter; padding:13px; border-radius:12px; border:none; cursor:pointer;}
    .nw-cs-save{background:#141a17; color:#cfe1d8; border:1px solid #253128 !important;}
    .nw-cs-share{background:#1f7a4d; color:#eafff3;}
    .nw-cs-share.off{background:#1a211d; color:#5f726a; cursor:not-allowed;}
    .nw-cs-sw{width:34px; height:34px; border-radius:7px; display:inline-block; overflow:hidden; border:1px solid rgba(255,255,255,.14); vertical-align:middle; background-size:cover;}
    .nw-cs-sw-lg{width:100%; height:120px; border-radius:8px; margin-bottom:10px; display:block;}
    .nw-cs-vid{position:absolute; inset:0; z-index:20; background:rgba(4,7,6,.95); display:flex; flex-direction:column; align-items:center; justify-content:center; gap:14px; padding:20px;}
    `;
    document.head.appendChild(s);
  }

  // ---- modal ---------------------------------------------------------------
  function buildModal() {
    injectStyles();
    modal = document.createElement('div');
    modal.id = 'nw-cs-modal';
    modal.innerHTML = `
      <div class="nw-cs-wrap">
        <div class="nw-cs-top">
          <h2>Share card</h2>
          <span style="display:flex; align-items:center;">
            <span class="nw-cs-coin" id="nw-cs-coin">&#129689; 0</span>
            <button class="nw-cs-x" id="nw-cs-close" title="Close">&#10005;</button>
          </span>
        </div>
        <p class="nw-cs-sub">Swipe left/right to cycle, up/down to switch category. Locked picks are dulled with a watermark; unlock spends coins and it's yours for good.</p>
        <div class="nw-cs-cats" id="nw-cs-cats">
          <div class="nw-cs-cat on" data-cat="0">Background</div>
          <div class="nw-cs-cat" data-cat="1">Frame</div>
          <div class="nw-cs-cat" data-cat="2">Stats</div>
        </div>
        <div class="nw-cs-stage" id="nw-cs-stage">
          <div class="nw-cs-chev l" id="nw-cs-chevL">&#8249;</div>
          <div class="nw-cs-chev r" id="nw-cs-chevR">&#8250;</div>
          <button class="nw-cs-ud up" id="nw-cs-up">&#9650;</button>
          <button class="nw-cs-ud down" id="nw-cs-down">&#9660;</button>
          <div class="nw-cs-slot" id="nw-cs-prev"></div>
          <div class="nw-cs-slot" id="nw-cs-next"></div>
          <div class="nw-cs-slot" id="nw-cs-cur"></div>
        </div>
        <div class="nw-cs-optname" id="nw-cs-optname"></div>
        <div class="nw-cs-dots" id="nw-cs-dots"></div>
        <div class="nw-cs-hint" id="nw-cs-hint"></div>
        <div class="nw-cs-actions">
          <button class="nw-cs-save" id="nw-cs-save">Save to my card</button>
          <button class="nw-cs-share" id="nw-cs-share">Share to&#8230;</button>
        </div>
      </div>`;
    document.body.appendChild(modal);

    modal.querySelector('#nw-cs-close').onclick = close;
    modal.querySelector('#nw-cs-cats').addEventListener('click', e => { const c = e.target.closest('.nw-cs-cat'); if (c) setCat(+c.dataset.cat); });
    modal.querySelector('#nw-cs-chevL').onclick = () => cycle(-1);
    modal.querySelector('#nw-cs-chevR').onclick = () => cycle(1);
    modal.querySelector('#nw-cs-up').onclick = () => setCat(cat - 1);
    modal.querySelector('#nw-cs-down').onclick = () => setCat(cat + 1);
    modal.querySelector('#nw-cs-save').onclick = saveToCard;
    modal.querySelector('#nw-cs-share').onclick = share;

    const stage = modal.querySelector('#nw-cs-stage');
    let sx = 0, sy = 0, tr = false;
    stage.addEventListener('touchstart', e => { sx = e.touches[0].clientX; sy = e.touches[0].clientY; tr = true; }, { passive: true });
    stage.addEventListener('touchend', e => {
      if (!tr) return; tr = false;
      const dx = e.changedTouches[0].clientX - sx, dy = e.changedTouches[0].clientY - sy;
      if (Math.abs(dx) > Math.abs(dy)) { if (Math.abs(dx) > 36) cycle(dx < 0 ? 1 : -1); }
      else { if (Math.abs(dy) > 36) setCat(cat + (dy < 0 ? 1 : -1)); }
    }, { passive: true });
    stage.querySelector('#nw-cs-cur').addEventListener('click', () => {
      const c = modal.querySelector('#nw-cs-cur .nw-cs-card'); if (!c) return;
      c.classList.remove('nw-cs-sweep'); void c.offsetWidth; c.classList.add('nw-cs-sweep');
    });
  }

  // ---- data assembly -------------------------------------------------------
  function ownedMap() { const p = myPlayer(); return (p && p.owned_items) || {}; }

  async function assembleOptions() {
    const owned = ownedMap();
    const sa = (typeof isSuperAdmin === 'function' && isSuperAdmin());
    const own = (id) => sa || !!owned[id];   // SuperAdmin owns everything
    let catalog = [];
    try { catalog = (typeof loadStoreCatalogOnce === 'function') ? (await loadStoreCatalogOnce()) : []; }
    catch (e) { catalog = []; }
    const active = catalog.filter(i => i.active !== false);

    const imgOf = (kind) => active
      .filter(i => (i.effect || {}).kind === kind && i.image_url)
      .map(i => ({ type: 'image', key: i.image_url, name: i.name, cost: Number(i.cost) || 0, item_id: i.item_id, owned: own(i.item_id) }));
    const presetOf = (kind) => active
      .filter(i => (i.effect || {}).kind === kind && (i.effect || {}).value)
      .map(i => ({ id: i.effect.value, name: i.name, cost: Number(i.cost) || 0, item_id: i.item_id, owned: own(i.item_id) }));

    // Background: free ids + premium presets + image uploads.
    bgOpts = FREE_BG.map(b => ({ type: 'preset', id: b.id, name: b.name, css: b.css, owned: true, cost: 0 }))
      .concat(presetOf('background_preset').map(p => {
        const b = PREMIUM_BG_BY_ID[p.id];
        return { type: 'preset', premium: true, id: p.id, name: p.name || (b ? b.name : p.id),
                 css: b ? b.css : bgCss('#101511', '#1a221d', '#243029'), anim: b ? b.anim : null,
                 cost: p.cost, item_id: p.item_id, owned: p.owned };
      }))
      .concat(imgOf('background_image'));

    // Frame: free Minimal + presets + image uploads.
    frameOpts = [{ type: 'minimal', name: 'Minimal', owned: true, cost: 0 }]
      .concat(presetOf('card_frame_preset').map(p => ({ type: 'preset', id: p.id, name: p.name || FRAME_NAME[p.id] || p.id, cost: p.cost, item_id: p.item_id, owned: p.owned })))
      .concat(imgOf('card_frame'));

    // Stats: free full/compact + premium card_layout presets.
    statsOpts = STATS_PRESETS.filter(s => s.free).map(s => ({ type: 'stats', id: s.id, name: s.name, owned: true, cost: 0 }))
      .concat(presetOf('card_layout').map(p => ({ type: 'stats', id: p.id, name: p.name || STATS_NAME[p.id] || p.id, cost: p.cost, item_id: p.item_id, owned: p.owned })));

    // Start on whatever is equipped.
    const me = myPlayer() || {};
    if (me.background_url) { const j = bgOpts.findIndex(o => o.type === 'image' && o.key === me.background_url); if (j >= 0) idx[0] = j; }
    else if (me.background_preset) { const j = bgOpts.findIndex(o => o.type === 'preset' && o.premium && o.id === me.background_preset); if (j >= 0) idx[0] = j; }
    else if (me.background_id) { const j = bgOpts.findIndex(o => o.type === 'preset' && !o.premium && o.id === me.background_id); if (j >= 0) idx[0] = j; }
    if (me.card_frame_url) { const j = frameOpts.findIndex(o => o.type === 'image' && o.key === me.card_frame_url); if (j >= 0) idx[1] = j; }
    else if (me.card_frame_preset) { const j = frameOpts.findIndex(o => o.type === 'preset' && o.id === me.card_frame_preset); if (j >= 0) idx[1] = j; }
    if (me.card_layout) { const j = statsOpts.findIndex(o => o.id === me.card_layout); if (j >= 0) idx[2] = j; }
  }

  async function loadStats() {
    const p = myPlayer();
    stats = {
      name: (p && p.name) || 'Player', nickname: (p && p.nickname) || '',
      rating: p ? Math.round(Number(p.rating) || 1000) : 1000,
      level: levelFromXp(p && p.xp), rank: rankOf(p),
      games: p ? (Number(p.games_played) || 0) : 0,
      avatarUrl: (p && p.avatar_url) ? srcOf(p.avatar_url) : null,
      wins: null, losses: null, pct: null, streak: null, trend: null,
      peak: null, bestStreak: 0, form10: [], topOpponent: null, topPartner: null
    };
    const id = meId(); if (!id) return;
    try {
      const res = await fetch(`${api()}/profile-secure/matches?profile_bundle_for=${id}`, { headers: authHeaders() });
      const b = await res.json();
      const rec = b.overall_record || {};
      if (typeof rec.total_wins === 'number') {
        stats.wins = rec.total_wins; stats.losses = rec.total_losses;
        const g = rec.total_wins + rec.total_losses; stats.pct = g ? Math.round(rec.total_wins / g * 100) : 0;
      }
      const form = (b.recent_form && b.recent_form.form) || [];
      if (form.length) {
        let sc = 0; for (let i = form.length - 1; i >= 0; i--) { if (form[i].result === 'W') sc++; else break; }
        stats.streak = sc;
        const last = form.slice(-8); let r = stats.rating, pts = [];
        for (let i = last.length - 1; i >= 0; i--) { pts.unshift(r); r -= Math.round(Number(last[i].delta) || 0); }
        stats.trend = pts;
      }
      const ach = b.achievements || {};
      stats.peak = Math.round(Number(ach.peak_rating) || stats.rating);
      stats.bestStreak = Number(ach.personal_best_streak) || 0;
      stats.form10 = form.slice(-10).map(f => f.result);
      stats.topOpponent = (b.top_opponents && b.top_opponents.opponents && b.top_opponents.opponents[0]) || null;
      try {
        const pr = await fetch(`${api()}/profile-secure/matches?partnerships_for=${id}`, { headers: authHeaders() });
        const pd = await pr.json();
        stats.topPartner = (pd.partnerships && pd.partnerships[0]) || null;
      } catch (e2) { /* partner card just hides */ }
    } catch (e) { /* rating/level/games only */ }
  }

  // ---- live preview --------------------------------------------------------
  function esc(t) { return String(t == null ? '' : t).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
  function trendBars(H, trend) {
    const vals = (trend && trend.length) ? trend : [980, 992, 985, 1004, 1012, 1006, 1020];
    const mn = Math.min(...vals) - 4, mx = Math.max(...vals) + 4, span = Math.max(1, mx - mn);
    return `<div class="nw-cs-trend" style="height:${H}px">` + vals.map(v => {
      const h = 30 + Math.round((v - mn) / span * 62);
      return `<i style="height:${h}%;background:linear-gradient(180deg,#7fd8a8,#1f5c3d)"></i>`;
    }).join('') + `</div>`;
  }
  function sparkSvg(trend) {
    const vals = (trend && trend.length) ? trend : [];
    if (vals.length < 2) return '';
    const w = 256, h = 84, mn = Math.min(...vals) - 4, mx = Math.max(...vals) + 4, span = Math.max(1, mx - mn);
    const P = vals.map((v, i) => [6 + i * ((w - 12) / (vals.length - 1)), h - 6 - ((v - mn) / span) * (h - 14)]);
    const line = P.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ');
    const area = line + ` L${P[P.length - 1][0].toFixed(1)} ${h} L${P[0][0].toFixed(1)} ${h} Z`;
    const pk = P[P.length - 1];
    return `<svg class="nw-cs-spark" viewBox="0 0 ${w} ${h}"><path d="${area}" fill="rgba(127,216,168,.15)"/><path d="${line}" fill="none" stroke="#7fd8a8" stroke-width="2.5" stroke-linejoin="round"/><circle cx="${pk[0].toFixed(1)}" cy="${pk[1].toFixed(1)}" r="4" fill="#eafff3" stroke="#7fd8a8" stroke-width="2"/></svg>`;
  }
  function donutSvg(pct) {
    const C = 2 * Math.PI * 46, p = Math.max(0, Math.min(100, pct || 0));
    return `<svg width="128" height="128" viewBox="0 0 120 120"><circle cx="60" cy="60" r="46" fill="none" stroke="#26332c" stroke-width="13"/><circle cx="60" cy="60" r="46" fill="none" stroke="#7fd8a8" stroke-width="13" stroke-linecap="round" stroke-dasharray="${(p / 100 * C).toFixed(1)} ${C.toFixed(1)}" transform="rotate(-90 60 60)"/><text x="60" y="56" text-anchor="middle" font-family="Rajdhani,Inter" font-weight="800" font-size="30" fill="#eafff3">${p}%</text><text x="60" y="76" text-anchor="middle" font-family="Inter" font-size="11" fill="#9fb3a8">win rate</text></svg>`;
  }
  function avatarHtml(s) {
    return s.avatarUrl ? `<span class="nw-cs-av" style="background-image:url('${s.avatarUrl}');"></span>` : `<span class="nw-cs-av">${initials(s.name)}</span>`;
  }
  function cardHTML(layout) {
    const s = stats, foot = '<div class="nw-cs-foot">&#9670; MATCHPOINT <span class="d">&middot;</span> NETWORTH &#9670;</div>';
    const rankTxt = s.rank ? `#${s.rank}` : '—';
    if (layout === 'compact') {
      return `<div style="display:flex;justify-content:center;margin-top:6px">${avatarHtml(s)}</div>`
        + `<div class="nw-cs-name" style="margin-top:12px">${esc(s.name)}</div><div class="nw-cs-handle">${s.nickname ? '@' + esc(s.nickname) : ''}</div>`
        + `<div class="nw-cs-big" style="margin-top:16px">${s.rating}</div><div class="nw-cs-cap" style="text-align:center;margin-top:2px">RATING &middot; LVL ${s.level} &middot; ${rankTxt}</div>`
        + (s.wins != null ? `<div class="nw-cs-rec">${s.wins}&#8211;${s.losses} &nbsp;&middot;&nbsp; ${s.pct}% win rate</div><div class="nw-cs-strk">&#128293; ${s.streak || 0} win streak &middot; ${s.games} games</div>` : `<div class="nw-cs-strk" style="margin-top:10px">${s.games} games played</div>`) + foot;
    }
    if (layout === 'curve') {
      const sp = sparkSvg(s.trend);
      return `<div class="nw-cs-rt"><div><div class="nw-cs-rating" style="font-size:44px">${s.rating}</div><div class="nw-cs-cap">RATING</div></div>${avatarHtml(s)}</div>`
        + `<div class="nw-cs-name" style="text-align:left;margin-top:10px;font-size:19px">${esc(s.name)} <span style="color:#8fa39a;font-weight:500;font-size:13px">${s.nickname ? '@' + esc(s.nickname) : ''}</span></div>`
        + (sp || `<div class="nw-cs-strk" style="margin:18px 0">Not enough matches for a trend yet.</div>`)
        + (sp ? `<div class="nw-cs-cap" style="text-align:center">RATING CURVE &middot; LAST ${s.trend.length}</div>` : '')
        + (s.wins != null ? `<div class="nw-cs-rec" style="font-size:14px;margin-top:12px">${s.wins}&#8211;${s.losses} &middot; ${s.pct}% &middot; &#128293; ${s.streak || 0}</div>` : '') + foot;
    }
    if (layout === 'donut') {
      return `<div class="nw-cs-rt"><div class="nw-cs-name" style="text-align:left;margin:2px 0 0;font-size:20px">${esc(s.name)}<div class="nw-cs-handle" style="text-align:left">${s.nickname ? '@' + esc(s.nickname) : ''} &middot; LVL ${s.level}</div></div>${avatarHtml(s)}</div>`
        + `<div style="display:flex; align-items:center; gap:14px; margin-top:14px;"><div>${donutSvg(s.pct)}</div><div><div class="nw-cs-rating" style="font-size:40px">${s.rating}</div><div class="nw-cs-cap">RATING &middot; RANK ${rankTxt}</div>`
        + `<div class="nw-cs-lr" style="margin-top:10px"><b>${s.wins != null ? s.wins + '&#8211;' + s.losses : '&#8211;'}</b> <span class="m">W&#8211;L</span></div><div class="nw-cs-lr"><b>${s.games}</b> <span class="m">GAMES &middot; &#128293;${s.streak || 0}</span></div></div></div>`
        + `<div class="nw-cs-div"></div><div class="nw-cs-cap" style="text-align:center">SEASON SUMMARY</div>` + foot;
    }
    if (layout === 'peak') {
      return `<div style="display:flex;justify-content:center;margin-top:6px">${avatarHtml(s)}</div>`
        + `<div class="nw-cs-name" style="margin-top:12px">${esc(s.name)}</div><div class="nw-cs-handle">${s.nickname ? '@' + esc(s.nickname) : ''}</div>`
        + `<div class="nw-cs-big" style="margin-top:16px;color:#ffd24a">${s.peak || s.rating}</div><div class="nw-cs-cap" style="text-align:center;margin-top:2px">PEAK RATING</div>`
        + `<div class="nw-cs-rec">Now ${s.rating} &middot; Rank ${rankTxt} &middot; LVL ${s.level}</div>` + foot;
    }
    if (layout === 'streak') {
      return `<div class="nw-cs-rt"><div><div class="nw-cs-rating" style="font-size:44px">${s.rating}</div><div class="nw-cs-cap">RATING &middot; ${rankTxt}</div></div>${avatarHtml(s)}</div>`
        + `<div class="nw-cs-name" style="text-align:left;margin-top:10px;font-size:19px">${esc(s.name)}</div>`
        + `<div class="nw-cs-grid" style="margin-top:16px"><div><div class="v">&#128293; ${s.streak || 0}</div><div class="nw-cs-cap">CURRENT STREAK</div></div><div><div class="v">&#127942; ${s.bestStreak || 0}</div><div class="nw-cs-cap">BEST STREAK</div></div></div>`
        + `<div class="nw-cs-div"></div><div class="nw-cs-rec" style="font-size:14px">${s.wins != null ? s.wins + '&#8211;' + s.losses + ' &middot; ' + s.pct + '% win rate' : s.games + ' games'}</div>` + foot;
    }
    if (layout === 'record') {
      return `<div class="nw-cs-rt"><div class="nw-cs-name" style="text-align:left;margin:2px 0 0;font-size:20px">${esc(s.name)}<div class="nw-cs-handle" style="text-align:left">${s.nickname ? '@' + esc(s.nickname) : ''}</div></div>${avatarHtml(s)}</div>`
        + `<div class="nw-cs-big" style="margin-top:14px">${s.wins != null ? s.wins + '&#8211;' + s.losses : '&#8211;'}</div><div class="nw-cs-cap" style="text-align:center">WIN &#8211; LOSS</div>`
        + `<div class="nw-cs-grid" style="margin-top:14px"><div><div class="v">${s.pct != null ? s.pct + '%' : '&#8211;'}</div><div class="nw-cs-cap">WIN RATE</div></div><div><div class="v">${s.games}</div><div class="nw-cs-cap">GAMES</div></div></div>` + foot;
    }
    if (layout === 'form') {
      const pills = (s.form10 && s.form10.length)
        ? '<div style="display:flex;gap:5px;justify-content:center;flex-wrap:wrap;margin-top:16px">' + s.form10.map(r => `<span style="width:26px;height:26px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font:700 12px Inter;color:#08120d;background:${r === 'W' ? '#7fd8a8' : '#e0725f'}">${r}</span>`).join('') + '</div>'
        : '<div class="nw-cs-strk" style="margin:18px 0">No recent matches yet.</div>';
      return `<div class="nw-cs-rt"><div><div class="nw-cs-rating" style="font-size:44px">${s.rating}</div><div class="nw-cs-cap">RATING &middot; ${rankTxt}</div></div>${avatarHtml(s)}</div>`
        + `<div class="nw-cs-name" style="text-align:left;margin-top:10px;font-size:19px">${esc(s.name)}</div>`
        + pills + `<div class="nw-cs-cap" style="text-align:center;margin-top:8px">LAST ${(s.form10 || []).length} RESULTS</div>`
        + (s.wins != null ? `<div class="nw-cs-rec" style="font-size:14px;margin-top:10px">${s.wins}&#8211;${s.losses} &middot; ${s.pct}% &middot; &#128293; ${s.streak || 0}</div>` : '') + foot;
    }
    if (layout === 'stuffed') {
      return `<div class="nw-cs-rt"><div><div class="nw-cs-rating">${s.rating}</div><div class="nw-cs-cap">RATING &middot; ${rankTxt} &middot; LVL ${s.level}</div></div>${avatarHtml(s)}</div>`
        + `<div class="nw-cs-name">${esc(s.name)}</div><div class="nw-cs-handle">${s.nickname ? '@' + esc(s.nickname) : ''}</div><div class="nw-cs-div"></div>`
        + `<div class="nw-cs-grid"><div><div class="v">${s.wins != null ? s.wins + '&#8211;' + s.losses : '&#8211;'}</div><div class="nw-cs-cap">W&#8211;L</div></div>`
        + `<div><div class="v">${s.pct != null ? s.pct + '%' : '&#8211;'}</div><div class="nw-cs-cap">WIN RATE</div></div>`
        + `<div><div class="v">${s.games}</div><div class="nw-cs-cap">GAMES</div></div>`
        + `<div><div class="v">${s.peak || s.rating}</div><div class="nw-cs-cap">PEAK</div></div>`
        + `<div><div class="v">&#128293; ${s.streak || 0}</div><div class="nw-cs-cap">STREAK</div></div>`
        + `<div><div class="v">&#127942; ${s.bestStreak || 0}</div><div class="nw-cs-cap">BEST</div></div></div>`
        + (s.trend && s.trend.length ? trendBars(40, s.trend) + `<div class="nw-cs-cap" style="text-align:center">RATING TREND</div>` : '') + foot;
    }
    if (layout === 'vs') {
      const o = s.topOpponent;
      return `<div class="nw-cs-rt"><div class="nw-cs-name" style="text-align:left;margin:2px 0 0;font-size:20px">${esc(s.name)}<div class="nw-cs-handle" style="text-align:left">${s.nickname ? '@' + esc(s.nickname) : ''}</div></div>${avatarHtml(s)}</div>`
        + (o ? `<div class="nw-cs-cap" style="text-align:center;margin-top:18px">HEAD-TO-HEAD vs</div><div class="nw-cs-name" style="margin-top:4px">${esc(o.opponent_name)}</div><div class="nw-cs-big" style="margin-top:10px">${o.wins}&#8211;${o.losses}</div><div class="nw-cs-cap" style="text-align:center">${o.win_rate}% &middot; ${o.matches} matches</div>` : `<div class="nw-cs-strk" style="margin:24px 0">Not enough matches for a rivalry yet.</div>`) + foot;
    }
    if (layout === 'partner') {
      const pt = s.topPartner;
      return `<div class="nw-cs-rt"><div class="nw-cs-name" style="text-align:left;margin:2px 0 0;font-size:20px">${esc(s.name)}<div class="nw-cs-handle" style="text-align:left">${s.nickname ? '@' + esc(s.nickname) : ''}</div></div>${avatarHtml(s)}</div>`
        + (pt ? `<div class="nw-cs-cap" style="text-align:center;margin-top:18px">BEST PARTNER</div><div class="nw-cs-name" style="margin-top:4px">${esc(pt.partner_name)}</div><div class="nw-cs-big" style="margin-top:10px">${pt.wins}&#8211;${pt.losses}</div><div class="nw-cs-cap" style="text-align:center">${pt.win_rate}% together &middot; ${pt.matches} matches</div>` : `<div class="nw-cs-strk" style="margin:24px 0">No partnership data yet.</div>`) + foot;
    }
    // full (default)
    const rec = (s.wins != null)
      ? `<div class="nw-cs-grid"><div><div class="v">${s.wins}-${s.losses}</div><div class="nw-cs-cap">W &#8211; L (${s.pct}%)</div></div><div><div class="v">${s.games}</div><div class="nw-cs-cap">GAMES</div></div><div><div class="v">&#128293; ${s.streak || 0}</div><div class="nw-cs-cap">WIN STREAK</div></div><div><div class="v">${s.pct}%</div><div class="nw-cs-cap">WIN RATE</div></div></div><div class="nw-cs-div"></div>`
      : `<div class="nw-cs-grid"><div><div class="v">${s.games}</div><div class="nw-cs-cap">GAMES</div></div><div><div class="v">LVL ${s.level}</div><div class="nw-cs-cap">LEVEL</div></div></div><div class="nw-cs-div"></div>`;
    return `<div class="nw-cs-rt"><div><div class="nw-cs-rating">${s.rating}</div><div class="nw-cs-cap">RATING</div><div class="nw-cs-lr"><span class="m">LVL</span> <b>${s.level}</b></div><div class="nw-cs-lr"><span class="m">RANK</span> <b>${rankTxt}</b></div></div>${avatarHtml(s)}</div>`
      + `<div class="nw-cs-name">${esc(s.name)}</div><div class="nw-cs-handle">${s.nickname ? '@' + esc(s.nickname) : ''}</div><div class="nw-cs-div"></div>${rec}`
      + trendBars(52, s.trend) + `<div class="nw-cs-cap" style="text-align:center">RATING TREND</div>` + foot;
  }

  function frameClass(fr) {
    if (fr.type === 'minimal') return 'nw-cs-min';
    if (fr.type === 'preset') return 'nw-cs-fr-' + fr.id;
    return '';
  }
  function svgTongue(bx, by, dx, dy, px, py, w, h) {
    const lx = bx - px * w / 2, ly = by - py * w / 2, rx = bx + px * w / 2, ry = by + py * w / 2;
    const tx = bx + dx * h, ty = by + dy * h;
    const c1x = bx - px * w * 0.1 + dx * h * 0.5, c1y = by - py * w * 0.1 + dy * h * 0.5;
    const c2x = bx + px * w * 0.1 + dx * h * 0.5, c2y = by + py * w * 0.1 + dy * h * 0.5;
    return `M${lx.toFixed(1)} ${ly.toFixed(1)} Q${c1x.toFixed(1)} ${c1y.toFixed(1)} ${tx.toFixed(1)} ${ty.toFixed(1)} Q${c2x.toFixed(1)} ${c2y.toFixed(1)} ${rx.toFixed(1)} ${ry.toFixed(1)} Z`;
  }
  const FLAME_BORDER_SVG = (function (W, H) {
    const m = 12, ei = 14; let outer = '', inner = '';
    function edge(x0, y0, x1, y1, dx, dy, px, py) {
      const len = Math.hypot(x1 - x0, y1 - y0), n = Math.max(4, Math.round(len / 38));
      for (let i = 0; i <= n; i++) {
        const bx = x0 + (x1 - x0) * (i / n), by = y0 + (y1 - y0) * (i / n), h = 32 + (i % 3) * 9, w = (len / n) * 1.25;
        outer += svgTongue(bx, by, dx, dy, px, py, w, h);
        inner += svgTongue(bx, by, dx, dy, px, py, w * 0.5, h * 0.58);
      }
    }
    edge(m + ei, ei, W - m - ei, ei, 0, 1, 1, 0);              // top
    edge(m + ei, H - ei, W - m - ei, H - ei, 0, -1, 1, 0);     // bottom
    edge(ei, m + ei, ei, H - m - ei, 1, 0, 0, 1);              // left
    edge(W - ei, m + ei, W - ei, H - m - ei, -1, 0, 0, 1);     // right
    return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" width="100%" height="100%" xmlns="http://www.w3.org/2000/svg"><path d="${outer}" fill="#ff4d0a"/><path d="${inner}" fill="#ffd24a"/></svg>`;
  })(300, 420);

  function buildSlot(slotEl, bgSel, frameSel, statsSel) {
    const isImg = frameSel.type === 'image';
    const isFlame = frameSel.type === 'preset' && frameSel.id === 'flame';
    slotEl.innerHTML = `<div class="nw-cs-frame ${frameClass(frameSel)}">
        <div class="nw-cs-card ${bgSel.anim || ''}">
        ${isFlame ? `<div class="nw-cs-flames">${FLAME_BORDER_SVG}</div>` : ''}
        <div class="nw-cs-content"></div>
        ${isImg ? `<div class="nw-cs-frimg" style="background-image:url('${srcOf(frameSel.key)}');"></div>` : ''}
        </div></div>`;
    const card = slotEl.querySelector('.nw-cs-card');
    card.style.background = (bgSel.type === 'preset')
      ? bgSel.css
      : `linear-gradient(rgba(7,12,10,.55),rgba(7,12,10,.72)), center/cover no-repeat url('${srcOf(bgSel.key)}')`;
    slotEl.querySelector('.nw-cs-content').innerHTML = cardHTML(statsSel.id);
    return card;
  }

  function sel(off) {
    const b = [...idx];
    if (off) { const l = listFor(cat).length; b[cat] = wrap(b[cat] + off, l); }
    return { bg: bgOpts[b[0]], frame: frameOpts[b[1]], stats: statsOpts[b[2]] };
  }

  function render() {
    if (!modal) return;
    const cur = sel(0);
    buildSlot(modal.querySelector('#nw-cs-cur'), cur.bg, cur.frame, cur.stats);
    buildSlot(modal.querySelector('#nw-cs-prev'), sel(-1).bg, sel(-1).frame, sel(-1).stats);
    buildSlot(modal.querySelector('#nw-cs-next'), sel(1).bg, sel(1).frame, sel(1).stats);
    modal.querySelectorAll('.nw-cs-cat').forEach(c => c.classList.toggle('on', +c.dataset.cat === cat));

    const anyLocked = !cur.bg.owned || !cur.frame.owned || !cur.stats.owned;
    const list = listFor(cat), active = list[idx[cat]];
    if (anyLocked) modal.querySelector('#nw-cs-cur .nw-cs-content').classList.add('dim');
    if (!active.owned) {
      const veil = document.createElement('div');
      veil.className = 'nw-cs-veil';
      veil.innerHTML = `<div class="wm">NETWORTH</div><div class="lk">&#128274; LOCKED</div>
        <div class="pc">${esc(active.name)} &middot; ${active.cost.toLocaleString()} &#129689;</div>
        <button class="nw-cs-unlock">Unlock now</button>`;
      modal.querySelector('#nw-cs-cur .nw-cs-card').appendChild(veil);
      veil.querySelector('.nw-cs-unlock').onclick = (e) => { e.stopPropagation(); unlock(active); };
    }

    const dots = modal.querySelector('#nw-cs-dots'); dots.innerHTML = '';
    list.forEach((o, k) => { const sp = document.createElement('span'); if (k === idx[cat]) sp.className = 'on'; else if (!o.owned) sp.className = 'lock'; dots.appendChild(sp); });
    modal.querySelector('#nw-cs-optname').textContent = active.name + (active.owned ? '' : ` · ${active.cost.toLocaleString()} 🪙`);

    const shareBtn = modal.querySelector('#nw-cs-share'), hint = modal.querySelector('#nw-cs-hint');
    if (anyLocked) {
      shareBtn.classList.add('off'); shareBtn.textContent = 'Unlock to share';
      const locks = []; if (!cur.bg.owned) locks.push('background'); if (!cur.frame.owned) locks.push('frame'); if (!cur.stats.owned) locks.push('stats');
      hint.innerHTML = `Can’t share while <b>${locks.join(' + ')}</b> ${locks.length > 1 ? 'are' : 'is'} locked`;
    } else { shareBtn.classList.remove('off'); shareBtn.textContent = 'Share to…'; hint.innerHTML = 'Ready to share'; }

    const p = myPlayer();
    modal.querySelector('#nw-cs-coin').textContent = '🪙 ' + ((p && Number(p.coins)) || 0).toLocaleString();
  }

  let animating = false;
  const SLOT_SP = 250;
  function cycle(d) {
    const list = listFor(cat);
    if (animating || list.length < 2) return;
    animating = true;
    const stage = modal.querySelector('#nw-cs-stage');
    const cur = modal.querySelector('#nw-cs-cur');
    const incoming = modal.querySelector(d > 0 ? '#nw-cs-next' : '#nw-cs-prev');
    cur.style.transform = `translate(calc(-50% ${d > 0 ? '-' : '+'} ${SLOT_SP}px),-50%) scale(.58)`;
    cur.style.opacity = '.12';
    incoming.style.transform = 'translate(-50%,-50%) scale(1.04)'; incoming.style.opacity = '1'; incoming.style.zIndex = '6';
    setTimeout(() => {
      stage.classList.add('nw-cs-noanim');
      [cur, incoming, modal.querySelector('#nw-cs-prev'), modal.querySelector('#nw-cs-next')]
        .forEach(s => { s.style.transform = ''; s.style.opacity = ''; s.style.zIndex = ''; });
      idx[cat] = wrap(idx[cat] + d, list.length);
      render();
      requestAnimationFrame(() => requestAnimationFrame(() => { stage.classList.remove('nw-cs-noanim'); animating = false; }));
    }, 320);
  }
  function setCat(c) { if (animating) return; cat = wrap(c, 3); render(); }

  // ---- purchase / equip ----------------------------------------------------
  async function unlock(opt) {
    if (opt.owned || !opt.item_id) return;
    const p = myPlayer();
    if (p && Number(p.coins || 0) < opt.cost) { nwAlertLocal(`Not enough coins — you need ${(opt.cost - Number(p.coins || 0)).toLocaleString()} more.`); return; }
    try {
      const r = await doAuthedFetch(`${api()}/store-purchase`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ item_id: opt.item_id }) });
      if (!r.ok) { nwAlertLocal('Purchase failed: ' + (r.error || 'try again')); return; }
      if (typeof loadPlayers === 'function') await loadPlayers();
      if (typeof updateHeaderCoins === 'function') updateHeaderCoins();
      opt.owned = true; render();
    } catch (e) { nwAlertLocal('Purchase failed: ' + e.message); }
  }

  async function saveToCard() {
    const cur = sel(0);
    if (!cur.bg.owned || !cur.frame.owned || !cur.stats.owned) { nwAlertLocal('Unlock the locked pick before saving.'); return; }
    const payload = {};
    if (cur.bg.type === 'image') payload.background_url = cur.bg.key;
    else if (cur.bg.premium) payload.background_preset = cur.bg.id;
    else payload.background_id = cur.bg.id;
    if (cur.frame.type === 'image') payload.card_frame_url = cur.frame.key;
    else if (cur.frame.type === 'preset') payload.card_frame_preset = cur.frame.id;
    else { payload.card_frame_url = ''; payload.card_frame_preset = ''; }
    payload.card_layout = cur.stats.id;
    const btn = modal.querySelector('#nw-cs-save'); btn.disabled = true; btn.textContent = 'Saving…';
    try {
      const r = await doAuthedFetch(`${api()}/update-my-card`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      if (!r.ok) { nwAlertLocal('Could not save: ' + (r.error || 'try again')); }
      else { if (typeof loadPlayers === 'function') await loadPlayers(); nwAlertLocal('Saved to your card.'); }
    } catch (e) { nwAlertLocal('Could not save: ' + e.message); }
    finally { btn.disabled = false; btn.textContent = 'Save to my card'; }
  }

  async function doAuthedFetch(url, opts) {
    if (typeof authedFetch === 'function') { const { res, data } = await authedFetch(url, opts); return { ok: res.ok, error: data && data.error }; }
    const res = await fetch(url, Object.assign({ headers: {} }, opts, { headers: Object.assign({}, authHeaders(), (opts && opts.headers) || {}) }));
    let error = ''; if (!res.ok) { try { error = (await res.json()).error; } catch (e) {} }
    return { ok: res.ok, error };
  }
  function nwAlertLocal(m) { if (typeof nwAlert === 'function') nwAlert(m); else alert(m); }

  // ---- canvas export -------------------------------------------------------
  function loadImg(src) {
    return new Promise((resolve) => {
      if (!src) { resolve(null); return; }
      const img = new Image(); img.crossOrigin = 'anonymous';
      img.onload = () => resolve(img); img.onerror = () => resolve(null); img.src = src;
    });
  }
  function drawCover(ctx, img, x, y, w, h) {
    const ir = img.width / img.height, r = w / h; let dw, dh;
    if (ir > r) { dh = h; dw = h * ir; } else { dw = w; dh = w / ir; }
    ctx.drawImage(img, x + (w - dw) / 2, y + (h - dh) / 2, dw, dh);
  }

  function drawStatsLayout(ctx, id, s, W, H, pad) {
    const cx0 = pad, avR = 92, acx = W - pad - avR, acy = pad + avR;
    function avatar(async_img) {
      ctx.save(); ctx.beginPath(); ctx.arc(acx, acy, avR, 0, Math.PI * 2);
      ctx.lineWidth = 5; ctx.strokeStyle = 'rgba(127,216,168,.6)'; ctx.stroke(); ctx.clip();
      if (async_img) ctx.drawImage(async_img, acx - avR, acy - avR, avR * 2, avR * 2);
      else { ctx.fillStyle = '#0e1a14'; ctx.fillRect(acx - avR, acy - avR, avR * 2, avR * 2); ctx.fillStyle = '#7fd8a8'; ctx.font = "800 62px 'Rajdhani', system-ui"; ctx.textAlign = 'center'; ctx.fillText(initials(s.name), acx, acy + 22); ctx.textAlign = 'left'; }
      ctx.restore();
    }
    function foot() { ctx.fillStyle = '#5bbf8a'; ctx.font = "700 30px 'Rajdhani', system-ui"; ctx.textAlign = 'center'; ctx.fillText('◆  MATCHPOINT · NETWORTH  ◆', W / 2, H - pad + 8); ctx.textAlign = 'left'; }

    if (id === 'donut') {
      ctx.textAlign = 'left'; ctx.fillStyle = '#fff'; ctx.font = "700 52px system-ui"; ctx.fillText(s.name, pad, pad + 44);
      ctx.fillStyle = '#8fa39a'; ctx.font = "500 28px system-ui"; ctx.fillText((s.nickname ? '@' + s.nickname : '') + ' · LVL ' + s.level, pad, pad + 84);
      avatar(s._av);
      const dcx = pad + 150, dcy = pad + 300;
      drawDonut(ctx, dcx, dcy, 120, s.pct != null ? s.pct : 0);
      ctx.textAlign = 'center'; ctx.fillStyle = '#eafff3'; ctx.font = "800 64px 'Rajdhani', system-ui"; ctx.fillText((s.pct != null ? s.pct : 0) + '%', dcx, dcy + 12);
      ctx.fillStyle = '#9fb3a8'; ctx.font = "500 24px system-ui"; ctx.fillText('win rate', dcx, dcy + 52); ctx.textAlign = 'left';
      const tx = pad + 320, ty = pad + 220;
      ctx.fillStyle = '#7fd8a8'; ctx.font = "800 80px 'Rajdhani', system-ui"; ctx.fillText(String(s.rating), tx, ty);
      ctx.fillStyle = '#93a89e'; ctx.font = "600 26px system-ui"; ctx.fillText('RATING · RANK ' + (s.rank ? '#' + s.rank : '—'), tx, ty + 36);
      ctx.fillStyle = '#fff'; ctx.font = "700 40px 'Rajdhani', system-ui"; ctx.fillText((s.wins != null ? s.wins + '–' + s.losses : '–') + '  W–L', tx, ty + 100);
      ctx.fillText(s.games + ' GAMES · 🔥' + (s.streak || 0), tx, ty + 152);
      foot(); return;
    }
    if (id === 'curve') {
      ctx.fillStyle = '#7fd8a8'; ctx.font = "800 110px 'Rajdhani', system-ui"; ctx.fillText(String(s.rating), pad, pad + 96);
      ctx.fillStyle = '#93a89e'; ctx.font = "600 26px system-ui"; ctx.fillText('RATING', pad + 4, pad + 138);
      avatar(s._av);
      ctx.fillStyle = '#fff'; ctx.font = "700 46px system-ui"; ctx.fillText(s.name, pad, pad + 250);
      if (s.trend && s.trend.length > 1) {
        drawSpark(ctx, pad, pad + 300, W - pad * 2, 340, s.trend);
        ctx.fillStyle = '#93a89e'; ctx.font = "600 26px system-ui"; ctx.textAlign = 'center';
        ctx.fillText('RATING CURVE · LAST ' + s.trend.length, W / 2, pad + 700); ctx.textAlign = 'left';
      } else { ctx.fillStyle = '#9fb3a8'; ctx.font = "500 30px system-ui"; ctx.fillText('Not enough matches for a trend yet.', pad, pad + 420); }
      if (s.wins != null) { ctx.fillStyle = '#dfece5'; ctx.font = "600 34px system-ui"; ctx.textAlign = 'center'; ctx.fillText(`${s.wins}–${s.losses} · ${s.pct}% · 🔥 ${s.streak || 0}`, W / 2, pad + 780); ctx.textAlign = 'left'; }
      foot(); return;
    }
    if (id === 'compact') {
      const cxr = W / 2;
      ctx.save(); ctx.beginPath(); ctx.arc(cxr, pad + 90, 88, 0, Math.PI * 2); ctx.lineWidth = 5; ctx.strokeStyle = 'rgba(127,216,168,.6)'; ctx.stroke(); ctx.clip();
      if (s._av) ctx.drawImage(s._av, cxr - 88, pad + 2, 176, 176);
      else { ctx.fillStyle = '#0e1a14'; ctx.fillRect(cxr - 88, pad + 2, 176, 176); ctx.fillStyle = '#7fd8a8'; ctx.font = "800 60px 'Rajdhani', system-ui"; ctx.textAlign = 'center'; ctx.fillText(initials(s.name), cxr, pad + 108); }
      ctx.restore();
      ctx.textAlign = 'center';
      ctx.fillStyle = '#fff'; ctx.font = "700 54px system-ui"; ctx.fillText(s.name, cxr, pad + 250);
      if (s.nickname) { ctx.fillStyle = '#8fa39a'; ctx.font = "500 32px system-ui"; ctx.fillText('@' + s.nickname, cxr, pad + 296); }
      ctx.fillStyle = '#7fd8a8'; ctx.font = "800 150px 'Rajdhani', system-ui"; ctx.fillText(String(s.rating), cxr, pad + 470);
      ctx.fillStyle = '#93a89e'; ctx.font = "600 26px system-ui"; ctx.fillText('RATING · LVL ' + s.level + ' · ' + (s.rank ? '#' + s.rank : '—'), cxr, pad + 512);
      if (s.wins != null) { ctx.fillStyle = '#dfece5'; ctx.font = "600 38px system-ui"; ctx.fillText(`${s.wins}–${s.losses}  ·  ${s.pct}% win rate`, cxr, pad + 592);
        ctx.fillStyle = '#9fb3a8'; ctx.font = "500 30px system-ui"; ctx.fillText('🔥 ' + (s.streak || 0) + ' win streak · ' + s.games + ' games', cxr, pad + 640); }
      else { ctx.fillStyle = '#9fb3a8'; ctx.font = "500 34px system-ui"; ctx.fillText(s.games + ' games played', cxr, pad + 600); }
      ctx.textAlign = 'left'; foot(); return;
    }

    if (id === 'peak') {
      const cxr = W / 2;
      ctx.save(); ctx.beginPath(); ctx.arc(cxr, pad + 90, 88, 0, Math.PI * 2); ctx.lineWidth = 5; ctx.strokeStyle = 'rgba(127,216,168,.6)'; ctx.stroke(); ctx.clip();
      if (s._av) ctx.drawImage(s._av, cxr - 88, pad + 2, 176, 176);
      else { ctx.fillStyle = '#0e1a14'; ctx.fillRect(cxr - 88, pad + 2, 176, 176); ctx.fillStyle = '#7fd8a8'; ctx.font = "800 60px 'Rajdhani', system-ui"; ctx.textAlign = 'center'; ctx.fillText(initials(s.name), cxr, pad + 108); }
      ctx.restore(); ctx.textAlign = 'center';
      ctx.fillStyle = '#fff'; ctx.font = "700 54px system-ui"; ctx.fillText(s.name, cxr, pad + 250);
      ctx.fillStyle = '#ffd24a'; ctx.font = "800 150px 'Rajdhani', system-ui"; ctx.fillText(String(s.peak || s.rating), cxr, pad + 448);
      ctx.fillStyle = '#93a89e'; ctx.font = "600 28px system-ui"; ctx.fillText('PEAK RATING', cxr, pad + 494);
      ctx.fillStyle = '#dfece5'; ctx.font = "600 34px system-ui"; ctx.fillText('Now ' + s.rating + ' \u00b7 Rank ' + (s.rank ? '#' + s.rank : '\u2014') + ' \u00b7 LVL ' + s.level, cxr, pad + 566);
      ctx.textAlign = 'left'; foot(); return;
    }
    if (id === 'streak') {
      ctx.fillStyle = '#7fd8a8'; ctx.font = "800 110px 'Rajdhani', system-ui"; ctx.fillText(String(s.rating), pad, pad + 96);
      ctx.fillStyle = '#93a89e'; ctx.font = "600 26px system-ui"; ctx.fillText('RATING \u00b7 ' + (s.rank ? '#' + s.rank : '\u2014'), pad + 4, pad + 138);
      avatar(s._av);
      ctx.fillStyle = '#fff'; ctx.font = "700 46px system-ui"; ctx.fillText(s.name, pad, pad + 250);
      const yy = pad + 330;
      ctx.fillStyle = '#fff'; ctx.font = "800 96px 'Rajdhani', system-ui"; ctx.fillText('\ud83d\udd25 ' + (s.streak || 0), pad, yy + 40);
      ctx.fillStyle = '#93a89e'; ctx.font = "600 26px system-ui"; ctx.fillText('CURRENT STREAK', pad + 4, yy + 88);
      ctx.fillStyle = '#fff'; ctx.font = "800 96px 'Rajdhani', system-ui"; ctx.fillText('\ud83c\udfc6 ' + (s.bestStreak || 0), pad, yy + 220);
      ctx.fillStyle = '#93a89e'; ctx.font = "600 26px system-ui"; ctx.fillText('BEST STREAK', pad + 4, yy + 268);
      if (s.wins != null) { ctx.fillStyle = '#dfece5'; ctx.font = "600 32px system-ui"; ctx.fillText(s.wins + '\u2013' + s.losses + ' \u00b7 ' + s.pct + '% win rate', pad, yy + 370); }
      foot(); return;
    }
    if (id === 'record') {
      ctx.textAlign = 'left';
      ctx.fillStyle = '#fff'; ctx.font = "700 52px system-ui"; ctx.fillText(s.name, pad, pad + 44);
      ctx.fillStyle = '#8fa39a'; ctx.font = "500 28px system-ui"; ctx.fillText((s.nickname ? '@' + s.nickname : '') + ' \u00b7 LVL ' + s.level, pad, pad + 84);
      avatar(s._av); ctx.textAlign = 'center';
      ctx.fillStyle = '#7fd8a8'; ctx.font = "800 150px 'Rajdhani', system-ui"; ctx.fillText(s.wins != null ? s.wins + '\u2013' + s.losses : '\u2013', W / 2, pad + 370);
      ctx.fillStyle = '#93a89e'; ctx.font = "600 28px system-ui"; ctx.fillText('WIN \u2013 LOSS', W / 2, pad + 418);
      const rc = [[(s.pct != null ? s.pct + '%' : '\u2013'), 'WIN RATE'], [String(s.games), 'GAMES']];
      const rcx = [W / 2 - 220, W / 2 + 220];
      rc.forEach(function (c, i) { ctx.fillStyle = '#fff'; ctx.font = "800 72px 'Rajdhani', system-ui"; ctx.fillText(c[0], rcx[i], pad + 560); ctx.fillStyle = '#93a89e'; ctx.font = "600 26px system-ui"; ctx.fillText(c[1], rcx[i], pad + 600); });
      ctx.textAlign = 'left'; foot(); return;
    }
    if (id === 'form') {
      ctx.fillStyle = '#7fd8a8'; ctx.font = "800 110px 'Rajdhani', system-ui"; ctx.fillText(String(s.rating), pad, pad + 96);
      ctx.fillStyle = '#93a89e'; ctx.font = "600 26px system-ui"; ctx.fillText('RATING \u00b7 ' + (s.rank ? '#' + s.rank : '\u2014'), pad + 4, pad + 138);
      avatar(s._av);
      ctx.fillStyle = '#fff'; ctx.font = "700 46px system-ui"; ctx.fillText(s.name, pad, pad + 250);
      const fr = s.form10 || [];
      if (fr.length) {
        const n = fr.length, gap = 20, size = Math.min(112, Math.floor((W - pad * 2 - gap * (n - 1)) / n));
        const totalW = n * size + (n - 1) * gap, startX = (W - totalW) / 2, ry = pad + 380;
        ctx.textAlign = 'center';
        fr.forEach(function (r, i) {
          const x = startX + i * (size + gap);
          ctx.fillStyle = r === 'W' ? '#7fd8a8' : '#e0725f';
          ctx.beginPath(); ctx.arc(x + size / 2, ry + size / 2, size / 2, 0, Math.PI * 2); ctx.fill();
          ctx.fillStyle = '#08120d'; ctx.font = "800 " + Math.floor(size * 0.5) + "px 'Rajdhani', system-ui"; ctx.fillText(r, x + size / 2, ry + size / 2 + size * 0.18);
        });
        ctx.fillStyle = '#93a89e'; ctx.font = "600 26px system-ui"; ctx.fillText('LAST ' + n + ' RESULTS', W / 2, ry + size + 60); ctx.textAlign = 'left';
      } else { ctx.fillStyle = '#9fb3a8'; ctx.font = "500 30px system-ui"; ctx.fillText('No recent matches yet.', pad, pad + 420); }
      foot(); return;
    }
    if (id === 'stuffed') {
      ctx.fillStyle = '#7fd8a8'; ctx.font = "800 120px 'Rajdhani', system-ui"; ctx.fillText(String(s.rating), pad, pad + 104);
      ctx.fillStyle = '#93a89e'; ctx.font = "600 26px system-ui"; ctx.fillText('RATING \u00b7 ' + (s.rank ? '#' + s.rank : '\u2014') + ' \u00b7 LVL ' + s.level, pad + 4, pad + 146);
      avatar(s._av);
      ctx.fillStyle = '#fff'; ctx.font = "700 50px system-ui"; ctx.fillText(s.name, pad, pad + 250);
      const sc = [[s.wins != null ? s.wins + '\u2013' + s.losses : '\u2013', 'W\u2013L'], [s.pct != null ? s.pct + '%' : '\u2013', 'WIN RATE'], [String(s.games), 'GAMES'], [String(s.peak || s.rating), 'PEAK'], ['\ud83d\udd25 ' + (s.streak || 0), 'STREAK'], ['\ud83c\udfc6 ' + (s.bestStreak || 0), 'BEST']];
      const scx = [pad, W / 2 + 10]; let sy = pad + 320;
      sc.forEach(function (c, i) { const x = scx[i % 2], cy = sy + Math.floor(i / 2) * 130;
        ctx.fillStyle = '#fff'; ctx.font = "700 56px 'Rajdhani', system-ui"; ctx.fillText(c[0], x, cy + 44);
        ctx.fillStyle = '#93a89e'; ctx.font = "600 24px system-ui"; ctx.fillText(c[1], x, cy + 82); });
      sy += 3 * 130 + 30;
      if (s.trend && s.trend.length > 1) { drawSpark(ctx, pad, sy, W - pad * 2, 220, s.trend); }
      foot(); return;
    }
    if (id === 'vs') {
      ctx.textAlign = 'left'; ctx.fillStyle = '#fff'; ctx.font = "700 52px system-ui"; ctx.fillText(s.name, pad, pad + 44);
      ctx.fillStyle = '#8fa39a'; ctx.font = "500 28px system-ui"; ctx.fillText((s.nickname ? '@' + s.nickname : '') + ' \u00b7 LVL ' + s.level, pad, pad + 84);
      avatar(s._av);
      const rec = s.topOpponent;
      if (rec) {
        ctx.textAlign = 'center';
        ctx.fillStyle = '#93a89e'; ctx.font = "600 30px system-ui"; ctx.fillText('HEAD-TO-HEAD vs', W / 2, pad + 340);
        ctx.fillStyle = '#fff'; ctx.font = "700 60px system-ui"; ctx.fillText(rec.opponent_name, W / 2, pad + 410);
        ctx.fillStyle = '#7fd8a8'; ctx.font = "800 150px 'Rajdhani', system-ui"; ctx.fillText(rec.wins + '\u2013' + rec.losses, W / 2, pad + 560);
        ctx.fillStyle = '#93a89e'; ctx.font = "600 30px system-ui"; ctx.fillText(rec.win_rate + '%  \u00b7 ' + rec.matches + ' matches', W / 2, pad + 620);
        ctx.textAlign = 'left';
      } else { ctx.fillStyle = '#9fb3a8'; ctx.font = "500 32px system-ui"; ctx.textAlign = 'center'; ctx.fillText('Not enough matches yet.', W / 2, pad + 420); ctx.textAlign = 'left'; }
      foot(); return;
    }
    if (id === 'partner') {
      ctx.textAlign = 'left'; ctx.fillStyle = '#fff'; ctx.font = "700 52px system-ui"; ctx.fillText(s.name, pad, pad + 44);
      ctx.fillStyle = '#8fa39a'; ctx.font = "500 28px system-ui"; ctx.fillText((s.nickname ? '@' + s.nickname : '') + ' \u00b7 LVL ' + s.level, pad, pad + 84);
      avatar(s._av);
      const rec = s.topPartner;
      if (rec) {
        ctx.textAlign = 'center';
        ctx.fillStyle = '#93a89e'; ctx.font = "600 30px system-ui"; ctx.fillText('BEST PARTNER', W / 2, pad + 340);
        ctx.fillStyle = '#fff'; ctx.font = "700 60px system-ui"; ctx.fillText(rec.partner_name, W / 2, pad + 410);
        ctx.fillStyle = '#7fd8a8'; ctx.font = "800 150px 'Rajdhani', system-ui"; ctx.fillText(rec.wins + '\u2013' + rec.losses, W / 2, pad + 560);
        ctx.fillStyle = '#93a89e'; ctx.font = "600 30px system-ui"; ctx.fillText(rec.win_rate + '% together \u00b7 ' + rec.matches + ' matches', W / 2, pad + 620);
        ctx.textAlign = 'left';
      } else { ctx.fillStyle = '#9fb3a8'; ctx.font = "500 32px system-ui"; ctx.textAlign = 'center'; ctx.fillText('Not enough matches yet.', W / 2, pad + 420); ctx.textAlign = 'left'; }
      foot(); return;
    }
    // full (default)
    ctx.fillStyle = '#7fd8a8'; ctx.font = "800 150px 'Rajdhani', system-ui"; ctx.fillText(String(s.rating), pad, pad + 128);
    ctx.fillStyle = '#93a89e'; ctx.font = "600 26px system-ui"; ctx.fillText('RATING', pad + 4, pad + 170);
    ctx.fillStyle = '#c9d6cf'; ctx.font = "400 30px system-ui"; ctx.fillText('LVL ', pad + 4, pad + 220); ctx.fillStyle = '#fff'; ctx.font = "700 30px system-ui"; ctx.fillText(String(s.level), pad + 66, pad + 220);
    ctx.fillStyle = '#c9d6cf'; ctx.font = "400 30px system-ui"; ctx.fillText('RANK ', pad + 4, pad + 262); ctx.fillStyle = '#fff'; ctx.font = "700 30px system-ui"; ctx.fillText(s.rank ? '#' + s.rank : '—', pad + 96, pad + 262);
    avatar(s._av);
    ctx.textAlign = 'center'; ctx.fillStyle = '#fff'; ctx.font = "700 58px system-ui"; ctx.fillText(s.name, W / 2, pad + 360);
    if (s.nickname) { ctx.fillStyle = '#8fa39a'; ctx.font = "500 34px system-ui"; ctx.fillText('@' + s.nickname, W / 2, pad + 404); }
    ctx.textAlign = 'left';
    let y = pad + 452;
    ctx.strokeStyle = 'rgba(127,216,168,.22)'; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(pad, y); ctx.lineTo(W - pad, y); ctx.stroke();
    y += 60;
    const cells = (s.wins != null)
      ? [[`${s.wins}-${s.losses}`, `W – L (${s.pct}%)`], [String(s.games), 'GAMES'], [`🔥 ${s.streak || 0}`, 'WIN STREAK'], [`${s.pct}%`, 'WIN RATE']]
      : [[String(s.games), 'GAMES'], [`LVL ${s.level}`, 'LEVEL']];
    const colX = [pad, W / 2 + 10];
    cells.forEach((c, i) => { const x = colX[i % 2], cy = y + Math.floor(i / 2) * 118;
      ctx.fillStyle = '#fff'; ctx.font = "700 52px 'Rajdhani', system-ui"; ctx.fillText(c[0], x, cy + 44);
      ctx.fillStyle = '#93a89e'; ctx.font = "600 24px system-ui"; ctx.fillText(c[1].toUpperCase(), x, cy + 80); });
    y += Math.ceil(cells.length / 2) * 118 + 30;
    if (s.trend && s.trend.length) {
      ctx.strokeStyle = 'rgba(127,216,168,.22)'; ctx.beginPath(); ctx.moveTo(pad, y); ctx.lineTo(W - pad, y); ctx.stroke(); y += 40;
      const mn = Math.min(...s.trend) - 4, mx = Math.max(...s.trend) + 4, span = Math.max(1, mx - mn);
      const bw = (W - pad * 2 - (s.trend.length - 1) * 14) / s.trend.length, bh = 150;
      s.trend.forEach((v, i) => { const h = 40 + (v - mn) / span * bh, bx = pad + i * (bw + 14);
        const g = ctx.createLinearGradient(0, y + bh - h, 0, y + bh); g.addColorStop(0, '#7fd8a8'); g.addColorStop(1, '#1f5c3d');
        ctx.fillStyle = g; rr(ctx, bx, y + (bh - h), bw, h, 8); ctx.fill(); });
      y += bh + 34; ctx.fillStyle = '#93a89e'; ctx.font = "600 24px system-ui"; ctx.textAlign = 'center'; ctx.fillText('RATING TREND · LAST ' + s.trend.length, W / 2, y); ctx.textAlign = 'left';
    }
    foot();
  }

  const CANVAS_BG_FREE = { court: ['#0b3018', '#12452a', '#1F7A4D'], plain: ['#0c110e', '#121814', '#1a221d'] };
  function CANVAS_BG_LOOKUP(bg) {
    return CANVAS_BG_FREE[bg.id] || (PREMIUM_BG_BY_ID[bg.id] && PREMIUM_BG_BY_ID[bg.id].canvas) || CANVAS_BG_FREE.court;
  }
  function paintBackground(ctx, bg, W, H, t) {
    if (bg.type === 'image') {
      if (bg._img) drawCover(ctx, bg._img, 0, 0, W, H);
      else { ctx.fillStyle = '#0b1712'; ctx.fillRect(0, 0, W, H); }
      return;
    }
    const c = CANVAS_BG_LOOKUP(bg), tt = (t == null) ? 0 : t;
    if (bg.anim === 'nw-cs-bg-drift') {
      const ang = tt * Math.PI * 2, cx = W / 2, cy = H / 2, R = Math.hypot(W, H) / 2;
      const g = ctx.createLinearGradient(cx - Math.cos(ang) * R, cy - Math.sin(ang) * R, cx + Math.cos(ang) * R, cy + Math.sin(ang) * R);
      g.addColorStop(0, c[0]); g.addColorStop(.5, c[1]); g.addColorStop(1, c[2]);
      ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
    } else if (bg.anim === 'nw-cs-bg-pulse') {
      const b = Math.sin(tt * Math.PI * 2) * 0.5 + 0.5, rad = W * (0.5 + 0.35 * b);
      const g = ctx.createRadialGradient(W / 2, H * 0.42, W * 0.04, W / 2, H * 0.5, rad);
      g.addColorStop(0, c[2]); g.addColorStop(.6, c[1]); g.addColorStop(1, c[0]);
      ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
    } else {
      const g = ctx.createLinearGradient(0, 0, W, H); g.addColorStop(0, c[0]); g.addColorStop(.55, c[1]); g.addColorStop(1, c[2]);
      ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
    }
  }
  function drawComposite(ctx, cur, W, H, pad, t) {
    paintBackground(ctx, cur.bg, W, H, t);
    const sc = ctx.createLinearGradient(0, 0, 0, H); sc.addColorStop(0, 'rgba(7,12,10,.3)'); sc.addColorStop(1, 'rgba(7,12,10,.55)');
    ctx.fillStyle = sc; ctx.fillRect(0, 0, W, H);
    ctx.textBaseline = 'alphabetic'; ctx.textAlign = 'left';
    ctx.save(); ctx.shadowColor = 'rgba(0,0,0,.6)'; ctx.shadowBlur = 10; ctx.shadowOffsetY = 2;
    drawStatsLayout(ctx, cur.stats.id, stats, W, H, pad);
    ctx.restore();
    if (cur.frame.type === 'minimal') { ctx.strokeStyle = 'rgba(127,216,168,.5)'; ctx.lineWidth = 6; rr(ctx, 14, 14, W - 28, H - 28, 46); ctx.stroke(); }
    else if (cur.frame.type === 'preset') { drawFramePreset(ctx, cur.frame.id, W, H, t); }
    else if (cur.frame._img) { ctx.drawImage(cur.frame._img, 0, 0, W, H); }
  }
  async function preloadAssets(cur) {
    stats._av = stats.avatarUrl ? await loadImg(stats.avatarUrl) : null;
    if (cur.bg.type === 'image') cur.bg._img = await loadImg(srcOf(cur.bg.key));
    if (cur.frame.type === 'image') cur.frame._img = await loadImg(srcOf(cur.frame.key));
  }
  const ANIM_FRAMES = { holo: 1, ice: 1, plasma: 1, flame: 1 };
  function isAnimatedSel(cur) {
    return (cur.frame.type === 'preset' && ANIM_FRAMES[cur.frame.id]) || (cur.bg.type === 'preset' && !!cur.bg.anim);
  }

  async function renderExport() {
    const cur = sel(0), W = 1080, H = 1350, pad = 72;
    const canvas = document.createElement('canvas'); canvas.width = W; canvas.height = H;
    const ctx = canvas.getContext('2d');
    if (document.fonts && document.fonts.ready) { try { await document.fonts.ready; } catch (e) {} }
    await preloadAssets(cur);
    drawComposite(ctx, cur, W, H, pad, null);      // null t = representative still
    return await new Promise(res => canvas.toBlob(b => res(b), 'image/png'));
  }

  async function renderAnimatedExport() {
    const cur = sel(0), W = 1080, H = 1350, pad = 72;
    const canvas = document.createElement('canvas'); canvas.width = W; canvas.height = H;
    const ctx = canvas.getContext('2d');
    if (document.fonts && document.fonts.ready) { try { await document.fonts.ready; } catch (e) {} }
    await preloadAssets(cur);
    const mimes = ['video/mp4;codecs=avc1.640028', 'video/mp4;codecs=avc1.42E01E', 'video/mp4;codecs=avc1', 'video/mp4;codecs=h264', 'video/mp4', 'video/webm;codecs=h264', 'video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm'];
    let mime = ''; for (const m of mimes) { if (window.MediaRecorder && MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(m)) { mime = m; break; } }
    if (!canvas.captureStream || !window.MediaRecorder || !mime) return null;   // unsupported -> caller falls back to PNG
    let rec;
    try { rec = new MediaRecorder(canvas.captureStream(30), { mimeType: mime, videoBitsPerSecond: 6000000 }); }
    catch (e) { return null; }
    const chunks = []; rec.ondataavailable = e => { if (e.data && e.data.size) chunks.push(e.data); };
    const DURATION = 2600;
    const done = new Promise(res => { rec.onstop = () => res(new Blob(chunks, { type: mime })); });
    drawComposite(ctx, cur, W, H, pad, 0);
    rec.start();
    const start = performance.now();
    await new Promise(resolve => {
      (function frame(now) {
        const el = now - start;
        drawComposite(ctx, cur, W, H, pad, (el % DURATION) / DURATION);
        if (el >= DURATION) resolve(); else requestAnimationFrame(frame);
      })(start);
    });
    try { rec.stop(); } catch (e) {}
    const blob = await done;
    return { blob, ext: mime.indexOf('video/mp4') === 0 ? 'mp4' : 'webm', mime };
  }

  function download(blob, fname) {
    const url = URL.createObjectURL(blob); const a = document.createElement('a');
    a.href = url; a.download = fname; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1500);
  }
  function showVideoResult(blob, ext, mime, fname) {
    const url = URL.createObjectURL(blob);
    const panel = document.createElement('div');
    panel.className = 'nw-cs-vid';
    panel.innerHTML = `
      <div style="font:700 15px Inter;">Your animated card</div>
      <video src="${url}" autoplay loop muted playsinline style="max-width:300px; width:100%; border-radius:16px;"></video>
      <div style="font-size:12px; color:#9aa8a0; text-align:center; max-width:300px; line-height:1.5;">Tap <b>Share</b> to send it, or <b>Save</b> / long-press the video to keep it${ext === 'webm' ? '. WebM may need you to save, then post it to Instagram/WhatsApp from your gallery.' : '.'}</div>
      <div style="display:flex; gap:10px; width:100%; max-width:300px;">
        <button class="nw-cs-save" id="nw-cs-vsave" style="flex:1; padding:12px; border-radius:11px; cursor:pointer;">Save</button>
        <button class="nw-cs-share" id="nw-cs-vshare" style="flex:1; padding:12px; border-radius:11px; cursor:pointer;">Share</button>
      </div>
      <button id="nw-cs-vclose" style="background:none; border:none; color:#8fa39a; cursor:pointer; font-size:13px;">Close</button>`;
    modal.appendChild(panel);
    const cleanup = () => { panel.remove(); setTimeout(() => URL.revokeObjectURL(url), 1500); };
    panel.querySelector('#nw-cs-vclose').onclick = cleanup;
    panel.querySelector('#nw-cs-vsave').onclick = async () => {
      // iOS ignores <a download> for a video blob (it just opens the clip
      // in a viewer), so the only real save-to-Photos/Files path there is
      // the share sheet. Everywhere else a direct download works.
      const isIOS = /iP(hone|ad|od)/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
      const file = new File([blob], fname, { type: mime });
      if (isIOS && navigator.canShare && navigator.canShare({ files: [file] })) {
        try { await navigator.share({ files: [file], title: 'My NetWorth card', text: 'My badminton stats card' }); return; }
        catch (e) { if (e && e.name === 'AbortError') return; }
      }
      try { download(blob, fname); } catch (e) { try { window.open(url, '_blank'); } catch (e2) {} }
    };
    panel.querySelector('#nw-cs-vshare').onclick = async () => {   // fresh user gesture -> share is allowed
      const file = new File([blob], fname, { type: mime });
      if (navigator.canShare && navigator.canShare({ files: [file] })) {
        try { await navigator.share({ files: [file], title: 'My NetWorth card', text: 'My badminton stats card' }); }
        catch (e) { if (!(e && e.name === 'AbortError')) nwAlertLocal('Couldn’t share directly — tap Save instead.'); }
      } else { nwAlertLocal('This browser can’t share a video directly — tap Save, then post it from your gallery.'); }
    };
  }
  async function share() {
    const btn = modal.querySelector('#nw-cs-share');
    if (btn.classList.contains('off')) { nwAlertLocal('Unlock the locked pick first — a locked pick can’t be shared.'); return; }
    const cur = sel(0), animated = isAnimatedSel(cur);
    const base = (stats.nickname || stats.name || 'player').replace(/[^a-z0-9]+/gi, '_');
    btn.disabled = true; const label = btn.textContent;
    try {
      if (animated) {
        btn.textContent = 'Recording…';
        const v = await renderAnimatedExport();
        if (v && v.blob && v.blob.size) { showVideoResult(v.blob, v.ext, v.mime, `${base}_networth.${v.ext}`); return; }
        // recording unsupported/empty -> fall through to a still PNG
      }
      btn.textContent = 'Rendering…';
      const blob = await renderExport();
      if (!blob) { nwAlertLocal('Could not generate the image.'); return; }
      const fname = `${base}_networth.png`;
      const file = new File([blob], fname, { type: 'image/png' });
      if (navigator.canShare && navigator.canShare({ files: [file] })) {
        try { await navigator.share({ files: [file], title: 'My NetWorth card', text: 'My badminton stats card' }); return; }
        catch (e) { if (e && e.name === 'AbortError') return; }
      }
      download(blob, fname);
    } catch (e) { if (!(e && e.name === 'AbortError')) nwAlertLocal('Share failed: ' + (e && e.message ? e.message : e)); }
    finally { btn.disabled = false; btn.textContent = label; }
  }

  // ---- open / close --------------------------------------------------------
  async function openCardShare() {
    if (!meId()) { nwAlertLocal('Log in and link a player to build your card.'); return; }
    if (!modal) buildModal();
    cat = 0; idx[0] = 0; idx[1] = 0; idx[2] = 0;
    modal.classList.add('open'); document.body.style.overflow = 'hidden';
    modal.querySelector('#nw-cs-optname').textContent = 'Loading…';
    await Promise.all([assembleOptions(), loadStats()]);
    render();
  }
  function close() { if (modal) modal.classList.remove('open'); document.body.style.overflow = ''; }

  injectStyles();               // ensure preview swatch classes exist for the store immediately
  window.openCardShare = openCardShare;
})();
