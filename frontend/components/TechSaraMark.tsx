/**
 * The TechSara brand mark — the official logo (public/techsara-mark.png,
 * cropped from the supplied techsara-logo.webp on 2026-07-23). Rendered as a
 * plain <img> so the standalone build needs no image-optimization server.
 */

export function TechSaraMark({ size = 56 }: { size?: number }) {
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src="/techsara-mark.png"
      alt="TechSara"
      width={size}
      height={size}
      className="shrink-0 select-none"
      draggable={false}
    />
  );
}
