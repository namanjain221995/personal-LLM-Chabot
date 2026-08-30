// @vitest-environment jsdom
/**
 * The agent plan reads as a pipeline, not a box (owner request 2026-08-29).
 *
 * It used to be a bordered "AGENT PLAN" card, always open, with no sense of
 * elapsed time — and the static "Planning the steps for this task" line stayed
 * above it for the whole run because nothing ever cleared it. These tests pin
 * the replacement: one collapsible summary that reports how long the work
 * took, a rail connecting the steps, and no card chrome.
 */
import { describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { AgentTimeline } from '../components/AgentTimeline';
import type { AgentStep } from '../lib/types';

const steps: AgentStep[] = [
  { id: 1, title: 'Define the Transformer Architecture', status: 'done' },
  { id: 2, title: 'Implement the Neural Network Layers', status: 'done', detail: 'wrote layers.py' },
  { id: 3, title: 'Construct the 200+ Layer Model', status: 'running' },
];

describe('AgentTimeline', () => {
  it('shows every step title', () => {
    cleanup();
    render(<AgentTimeline steps={steps} />);
    for (const s of steps) expect(screen.getByText(s.title)).toBeTruthy();
  });

  it('summarises the work and collapses the whole pipeline', () => {
    cleanup();
    render(<AgentTimeline steps={steps} />);
    const summary = screen.getByRole('button', { expanded: true });
    expect(summary.textContent).toContain('Working');
    fireEvent.click(summary);
    expect(screen.queryByText(steps[0].title)).toBeNull();
    fireEvent.click(screen.getByRole('button', { expanded: false }));
    expect(screen.getByText(steps[0].title)).toBeTruthy();
  });

  it('reports elapsed time once nothing is running', () => {
    cleanup();
    const finished = steps.map((s) => ({ ...s, status: 'done' as const }));
    render(<AgentTimeline steps={finished} />);
    // The summary is the FIRST button; an expandable step is a button too.
    // No step ever ran in front of this render (a reloaded message), so it
    // states the count rather than inventing a duration.
    expect(screen.getAllByRole('button')[0].textContent).toContain('3 steps');
  });

  it('renders no card chrome — the chat body is the surface', () => {
    cleanup();
    const { container } = render(<AgentTimeline steps={steps} />);
    expect(container.querySelector('.border-border.bg-surface\\/60')).toBeNull();
    expect(screen.queryByText(/agent plan/i)).toBeNull();
  });

  it('renders nothing at all with no steps', () => {
    cleanup();
    const { container } = render(<AgentTimeline steps={[]} />);
    expect(container.firstChild).toBeNull();
  });
  it('counts every step on a reloaded transcript, failed ones included', () => {
    cleanup();
    // Review round 2026-08-30: the label counted only 'done', so a plan with
    // a failed step visibly disagreed with its own list ("2 steps" over 3).
    const mixed: AgentStep[] = [
      { id: 1, title: 'a', status: 'done' },
      { id: 2, title: 'b', status: 'failed' },
      { id: 3, title: 'c', status: 'done' },
    ];
    render(<AgentTimeline steps={mixed} />);
    expect(screen.getAllByRole('button')[0].textContent).toContain('3 steps');
  });
});
