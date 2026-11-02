import Link from "next/link";

// The floating header bar shared by the search page and the 404. `current` marks the brand link
// as the page the shopper is on.
export function SiteHeader({ current = false }: { current?: boolean }) {
  return (
    <header className="site-header">
      <div className="header-bar">
        <Link className="brand" href="/" aria-current={current ? "page" : undefined}>
          <span className="brand-mark button-md" aria-hidden="true">F<span>F</span></span>
          <span className="brand-name body-4">Fashion Finder</span>
        </Link>
        <span className="market-label caption-3">Hungary · HUF</span>
      </div>
    </header>
  );
}
