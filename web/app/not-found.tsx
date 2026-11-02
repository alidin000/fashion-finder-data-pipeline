import type { Metadata } from "next";
import Link from "next/link";
import { Icon } from "./_components/icons";
import { SiteHeader } from "./_components/site-header";

export const metadata: Metadata = {
  title: "Page not found | Fashion Finder",
  description: "This address is not part of Fashion Finder.",
};

export default function NotFound() {
  return (
    <>
      <SiteHeader />
      <main id="main" tabIndex={-1}>
        <div className="site-shell">
          <section className="not-found" aria-labelledby="not-found-title">
            <span className="empty-icon" aria-hidden="true"><Icon name="coat-hanger" size={24} /></span>
            <span className="eyebrow caption-3">Error 404</span>
            <h1 id="not-found-title" className="hero-title heading-4 md:heading-2">This page does not exist.</h1>
            <p className="hero-lede title-2">The link may be old or mistyped. Go back to search current offers.</p>
            <Link className="btn btn-primary button-md" href="/"><Icon name="arrow-left" />Back to search</Link>
          </section>
        </div>
      </main>
    </>
  );
}
