
## This is review round 2

Round 1 (reviews/review-1.md) found no blocking issues and made four design recommendations. All four were addressed; verify each and say whether it is resolved:

1. Calibrate on phones: the servo table now has a "swipe sideways" hint, edge shadows, and a pinned servo-name column (`calibrate.tsx`, `more.css`; see `reviews/shots/flow-390-calibrate-scrolled.png`).
2. Record trim: minus and plus buttons and a number box per edge, in steps of 0.1 s, beside each slider (`record.tsx`; see `reviews/shots/flow-390-record-trim.png`).
3. Library on touch: a tap previews and reveals a separate "Play on robot" button; it no longer opens the confirm (`library.tsx`; see `reviews/shots/flow-390-library-tapped.png`).
4. Session replay: a "Replaying <exercise>, set <n>" line sits above the replay controls (`sessions.tsx`; see `reviews/shots/flow-390-replay-set.png`).

`dashboard/web/e2e/touch.mjs` checks these on a 390 px touch viewport. Report only recommendations that still stand or are new. If the product meets the bar, say so plainly and write "None." under the section.
