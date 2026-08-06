'use client';

/**
 * Chart-scoped React error boundary.
 *
 * There was no error boundary anywhere in this app. A throw inside the
 * chart renderer therefore unmounted the entire React tree — the answer,
 * the sources, the whole conversation — and left a white page. A chart is
 * the least important thing on screen; it must not be able to take the
 * most important thing with it.
 *
 * Scope is one chart. Everything outside it keeps rendering, and the
 * fallback tells the reader where their numbers still are.
 */

import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props {
  children: ReactNode;
  /** Rendered in place of the chart. Defaults to a compact notice. */
  fallback?: ReactNode;
}

interface State {
  failed: boolean;
}

export class ChartErrorBoundary extends Component<Props, State> {
  state: State = { failed: false };

  static getDerivedStateFromError(): State {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // No telemetry platform exists here, and this is not the place to
    // invent one. In development the stack is genuinely useful; in
    // production it is noise at best and internals at worst, so users
    // never see it either way — the fallback below says what happened
    // without a stack trace.
    if (process.env.NODE_ENV !== 'production') {
      // eslint-disable-next-line no-console
      console.error('[chart] render failed', error, info.componentStack);
    }
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      this.props.fallback ?? (
        <p className="text-sm text-muted">
          Chart could not be displayed. The figures are in the Data tab.
        </p>
      )
    );
  }
}
