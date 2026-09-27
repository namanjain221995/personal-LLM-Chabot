/**
 * Harness: the REAL <MermaidBlock> from the worktree, in a real Chromium, in a
 * chat-width column. Source and theme come from the query string so one page
 * serves the before/after pair.
 */
import { createRoot } from 'react-dom/client';
import { MermaidBlock } from '@/components/MermaidBlock';
import { Providers } from '@/components/Providers';

const q = new URLSearchParams(location.search);
const raw = atob(q.get('src') || '');
const code = new TextDecoder().decode(Uint8Array.from(raw, (c) => c.charCodeAt(0)));
const width = q.get('w') || '702';
const theme = q.get('theme') === 'light' ? 'light' : 'dark';

document.documentElement.classList.remove('dark', 'light');
document.documentElement.classList.add(theme);
document.documentElement.style.colorScheme = theme;

const host = document.getElementById('root')!;
host.style.width = `${width}px`;
createRoot(host).render(
  <Providers>
    <div className="chat-answer">
      <MermaidBlock code={code} />
    </div>
  </Providers>,
);
