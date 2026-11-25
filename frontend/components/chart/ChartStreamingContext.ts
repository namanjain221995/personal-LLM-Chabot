'use client';

/**
 * Whether the message around a chart is still streaming (charts design v2
 * §10, C10).
 *
 * A Mermaid `pie` prefix that ends on a newline is a valid pie, and an
 * `xychart` is valid after its first complete series, so a block drawn while
 * tokens arrive would flash charts with wrong numbers (one slice = 100%), and
 * a failing prefix would fall through to mermaid and load it (~1 MB).
 * MessageRow provides its `streaming` flag here; a chart-headed block renders a
 * reserved "Drawing the chart…" box until it is false. Anything rendered
 * without a provider (SharedConversation, tests) counts as settled.
 */

import { createContext, useContext } from 'react';

export const ChartStreamingContext = createContext<boolean>(false);

export function useChartStreaming(): boolean {
  return useContext(ChartStreamingContext);
}
