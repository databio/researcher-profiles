/**
 * The app mark: a rounded badge holding a stylized profile silhouette.
 *
 * Colors come from theme tokens via SVG `fill` attributes (not an inline
 * `style` object), so it stays legible in both themes and does not trip the
 * theme.test inline-style lock. The badge fill is the non-inverting accent
 * solid, so the mark reads the same in light and dark.
 */
interface Props {
  size?: number;
}

export function Logo({ size = 26 }: Props) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      role="img"
      aria-label={__RP_APP_NAME__}
      xmlns="http://www.w3.org/2000/svg"
    >
      <rect x="0" y="0" width="32" height="32" rx="8" fill="var(--rp-color-accent-solid)" />
      {/* head */}
      <circle cx="16" cy="12.5" r="4.6" fill="var(--rp-color-on-solid)" />
      {/* shoulders */}
      <path
        d="M7.5 25.5c0-4.7 3.8-8 8.5-8s8.5 3.3 8.5 8"
        fill="none"
        stroke="var(--rp-color-on-solid)"
        strokeWidth="2.6"
        strokeLinecap="round"
      />
    </svg>
  );
}
