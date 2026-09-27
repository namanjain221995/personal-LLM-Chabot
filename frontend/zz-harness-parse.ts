import mermaid from 'mermaid';
import { mermaidTheme, prepareDiagramSource } from '@/lib/mermaidTheme';
mermaid.initialize(mermaidTheme('dark'));
(window as unknown as Record<string, unknown>).__probe = async (src: string) => {
  const prepared = prepareDiagramSource(src, 'dark');
  try {
    await mermaid.render('probe-' + Math.random().toString(36).slice(2), prepared);
    return { ok: true, prepared };
  } catch (e) {
    return { ok: false, prepared, message: e instanceof Error ? e.message : String(e) };
  }
};
