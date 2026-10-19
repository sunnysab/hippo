import { type ReactNode, useEffect, useRef } from 'react';
import { useLocation } from 'react-router-dom';
import { TopBar } from './TopBar';
import { useToast } from '../hooks/useToast';
import { onToast } from '../utils/events';

/**
 * The signed-in frame every page renders inside.
 *
 * It stays out of infrastructure: the WeChat daemon's session and the site-wide
 * sync state belong to the admin source page, not to a shell that wraps every
 * reader page and would otherwise probe them on each one.
 */
export function AppShell({ children }: { children: ReactNode }) {
  const location = useLocation();
  const { showToast } = useToast();
  const appRef = useRef<HTMLDivElement>(null);
  const topbarRef = useRef<HTMLElement>(null);

  const currentTab = location.pathname.split('/').filter(Boolean)[0] || 'groups';

  useEffect(() => {
    const app = appRef.current;
    const topbar = topbarRef.current;
    if (!app || !topbar) return;

    const updateTopbarHeight = () => {
      app.style.setProperty('--app-topbar-height', `${Math.ceil(topbar.getBoundingClientRect().height)}px`);
    };

    updateTopbarHeight();
    const observer = new ResizeObserver(updateTopbarHeight);
    observer.observe(topbar);
    window.addEventListener('resize', updateTopbarHeight);

    return () => {
      observer.disconnect();
      window.removeEventListener('resize', updateTopbarHeight);
    };
  }, []);

  // Listen for toast events from the API layer
  useEffect(() => {
    return onToast(showToast);
  }, [showToast]);

  return (
    <div className="app" ref={appRef}>
      <TopBar topbarRef={topbarRef} currentTab={currentTab} />
      <main className="content">
        <div id="view-root">{children}</div>
      </main>
    </div>
  );
}
