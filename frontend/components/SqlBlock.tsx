'use client';

/**
 * "View SQL" section body (§9): highlighted query, copy button, and the
 * exact trust caption. Highlighting is a tiny hand-rolled tokenizer — no
 * heavyweight syntax library for one dialect.
 */

import { useMemo, type ReactNode } from 'react';
import { CopyButton } from './CopyButton';

const KEYWORDS = new Set(
  (
    'select from where group by order having limit offset join inner left ' +
    'right full outer cross on as and or not in is null like ilike between ' +
    'case when then else end union all distinct with over partition range ' +
    'rows filter asc desc cast exists date interval extract using natural ' +
    'strftime count sum avg min max coalesce round'
  ).split(' '),
);

type Token = { kind: 'kw' | 'str' | 'num' | 'com' | 'plain'; text: string };

function tokenizeSql(sql: string): Token[] {
  const tokens: Token[] = [];
  const re =
    /(--[^\n]*)|('(?:[^']|'')*')|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)|(\s+|.)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(sql)) !== null) {
    if (m[1]) tokens.push({ kind: 'com', text: m[1] });
    else if (m[2]) tokens.push({ kind: 'str', text: m[2] });
    else if (m[3]) tokens.push({ kind: 'num', text: m[3] });
    else if (m[4]) {
      tokens.push({
        kind: KEYWORDS.has(m[4].toLowerCase()) ? 'kw' : 'plain',
        text: m[4],
      });
    } else tokens.push({ kind: 'plain', text: m[5] });
  }
  return tokens;
}

export function SqlBlock({ sql }: { sql: string }) {
  const highlighted = useMemo<ReactNode[]>(
    () =>
      tokenizeSql(sql).map((t, i) =>
        t.kind === 'plain' ? (
          t.text
        ) : (
          <span key={i} className={`tok-${t.kind}`}>
            {t.text}
          </span>
        ),
      ),
    [sql],
  );

  return (
    <div>
      <div className="code-block overflow-hidden rounded-ts border border-border bg-surface">
        <div className="flex items-center justify-between border-b border-border bg-surface-2/60 px-3 py-1.5">
          <span className="font-mono text-[11px] uppercase tracking-wide text-faint">
            DuckDB SQL
          </span>
          <CopyButton text={sql} label="Copy SQL" />
        </div>
        <pre tabIndex={0}>
          <code>{highlighted}</code>
        </pre>
      </div>
      <p className="mt-2 text-xs text-muted">
        This exact query produced the numbers above
      </p>
    </div>
  );
}
