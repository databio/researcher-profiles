/**
 * The app mark. Colors are theme tokens on SVG `fill` attributes, not an inline
 * `style`, so it passes the theme.test inline-style lock.
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
      <circle cx="16" cy="12.5" r="4.6" fill="var(--rp-color-on-solid)" />
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
