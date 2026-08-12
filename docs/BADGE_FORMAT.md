# NetWorth badge format (Apple-Fitness style)

A reference for making season/achievement badges that read cleanly at small sizes,
the way Apple Fitness / Clash Royale badges do. The working example in code is
`seasonMedallion(rank, size)` in `frontend/js/app.js` (the gold/silver/bronze
top-3 hexagons on the Season board).

## The recipe (what makes them look "Apple")

1. **One shape, filled edge-to-edge.** A flat-top hexagon (or a circle for
   "limited edition" style). No borders around the badge itself, no drop shadow.
2. **One vertical gradient**, light at the top going darker at the bottom, as if
   lit from above. Two or three stops, same hue family. This single gradient is
   what sells the "metal medal" look.
3. **One glyph, centered, generous padding.** A single silhouette (trophy, flame,
   shuttle, number, crown). It sits in the middle third of the badge with lots of
   empty space around it - restraint is the whole aesthetic. Never more than one
   idea per badge.
4. **A thin inner highlight stroke** just inside the edge (white at ~50% opacity),
   which gives the beveled/enameled feel without a heavy outline.
5. **Legible at 24-28px.** Design it small. If the glyph is unreadable at 26px,
   it's too detailed - simplify until it works as a silhouette.

## Palette (tiers)

| Tier / theme     | top stop  | mid stop  | bottom stop |
|------------------|-----------|-----------|-------------|
| Gold (1st)       | `#FFE27A` | `#F4B23E` | `#8A5A12`   |
| Silver (2nd)     | `#F2F3F6` | `#C7CBD4` | `#7C818C`   |
| Bronze (3rd)     | `#F0B98A` | `#CE8A4E` | `#7A4A24`   |
| Emerald (streak) | `#8CE0B0` | `#37B87C` | `#126B45`   |
| Sapphire (mix)   | `#8FC7FF` | `#3E8EF4` | `#144F9E`   |
| Ruby (comeback)  | `#FF9AA6` | `#F0475E` | `#8A1424`   |

Glyph fill: a dark tint of the badge hue (e.g. `#3a2a08` on gold) so it reads as
"stamped in," not a separate color.

## Drop-in SVG template

Copy this, swap the three gradient stops for the tier, and replace the `<!-- GLYPH -->`
line with your own centered silhouette (keep it inside roughly x/y 30-70).

```svg
<svg width="26" height="26" viewBox="0 0 100 100" aria-hidden="true">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0"    stop-color="#FFE27A"/>
      <stop offset="0.55" stop-color="#F4B23E"/>
      <stop offset="1"    stop-color="#8A5A12"/>
    </linearGradient>
  </defs>
  <!-- badge shape: flat-top hexagon -->
  <polygon points="30,6 70,6 94,50 70,94 30,94 6,50" fill="url(#bg)"/>
  <!-- inner highlight stroke -->
  <polygon points="30,6 70,6 94,50 70,94 30,94 6,50" fill="none"
           stroke="rgba(255,255,255,0.55)" stroke-width="3"/>
  <!-- GLYPH: one centered silhouette, ~x/y 30-70. e.g. a rank number: -->
  <text x="50" y="50" dy="0.35em" text-anchor="middle"
        font-size="42" font-weight="800" fill="#3a2a08">1</text>
</svg>
```

Circle variant (for "limited edition" one-offs, like Apple's round badges): replace
both `<polygon>` lines with
`<circle cx="50" cy="50" r="46" fill="url(#bg)"/>` and
`<circle cx="50" cy="50" r="46" fill="none" stroke="rgba(255,255,255,0.55)" stroke-width="3"/>`.

## Sizes used in the app

- In tables / inline (Season board rank): **24-28px**.
- On the Player Card as an earned badge: **40-56px**.
- Hero / modal header: **72-96px**.

Always author at `viewBox="0 0 100 100"` and scale via `width`/`height` so one
source renders crisp everywhere.

## Adding a new badge in code

Extend the palette map in `seasonMedallion` (or a future `badgeSvg(kind)`),
add your glyph markup, and reference it wherever badges render. Keep the shape +
gradient + single-glyph rules and any new badge will sit consistently next to the
existing ones.
