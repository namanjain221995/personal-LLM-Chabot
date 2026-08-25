// @vitest-environment jsdom
/**
 * The 404-style error page.
 *
 * Half of this file asserts what is NOT on screen. The page replaced an
 * inline banner plus a "Technical details" disclosure that dumped the raw
 * upstream payload into the thread, and the requirement is that no such thing
 * can come back: not expanded, not collapsed, not in a title attribute.
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import ChatErrorPage from '../components/ChatErrorPage';
import { toClientError } from '../lib/errorTypes';

afterEach(cleanup);

describe('ChatErrorPage', () => {
  it('shows the real status, the friendly title and the description', () => {
    render(
      <ChatErrorPage
        error={toClientError(503)}
        onRetry={() => {}}
        onReturn={() => {}}
      />,
    );
    expect(screen.getByTestId('chat-error-status').textContent).toBe('503');
    expect(screen.getByText('AI service unavailable')).toBeTruthy();
    expect(
      screen.getByText(/AI service is temporarily unavailable/i),
    ).toBeTruthy();
  });

  it.each([
    [404, '404', "We couldn't find the page"],
    [500, '500', 'Something went wrong'],
    [502, '502', 'Model server unavailable'],
    [504, '504', 'Request timed out'],
  ])('renders %i as its own status and title', (status, display, title) => {
    render(
      <ChatErrorPage error={toClientError(status)} onReturn={() => {}} />,
    );
    expect(screen.getByTestId('chat-error-status').textContent).toBe(display);
    expect(screen.getByText(title)).toBeTruthy();
  });

  it('says "Error" — not 404 — when no status was ever received', () => {
    render(<ChatErrorPage error={toClientError(null)} onReturn={() => {}} />);
    expect(screen.getByTestId('chat-error-status').textContent).toBe('Error');
    expect(screen.queryByText('404')).toBeNull();
    expect(screen.getByText('Connection unavailable')).toBeTruthy();
  });

  it('offers Retry when retryable and always offers Return to chat', () => {
    const onRetry = vi.fn();
    const onReturn = vi.fn();
    render(
      <ChatErrorPage
        error={toClientError(503)}
        onRetry={onRetry}
        onReturn={onReturn}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: /retry/i }));
    fireEvent.click(screen.getByRole('button', { name: /return to chat/i }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(onReturn).toHaveBeenCalledTimes(1);
  });

  it('hides Retry when no retry handler is supplied', () => {
    render(<ChatErrorPage error={toClientError(404)} onReturn={() => {}} />);
    expect(screen.queryByRole('button', { name: /retry/i })).toBeNull();
    expect(screen.getByRole('button', { name: /return to chat/i })).toBeTruthy();
  });

  it('announces itself to assistive tech', () => {
    render(<ChatErrorPage error={toClientError(500)} onReturn={() => {}} />);
    expect(screen.getByRole('alert')).toBeTruthy();
  });

  // --- the hard requirement: nothing technical, anywhere ---
  it('renders no technical details for any status', () => {
    for (const status of [null, 400, 404, 500, 502, 503, 504]) {
      cleanup();
      render(<ChatErrorPage error={toClientError(status)} onReturn={() => {}} />);
      expect(screen.queryByText(/technical details/i)).toBeNull();
      expect(screen.queryByText(/request id/i)).toBeNull();
      expect(screen.queryByText(/vllm/i)).toBeNull();
      expect(screen.queryByText(/localhost:8080/i)).toBeNull();
      expect(screen.queryByText(/traceback/i)).toBeNull();
      expect(screen.queryByText(/orchestrator/i)).toBeNull();
      expect(screen.queryByText(/ECONNREFUSED/i)).toBeNull();
    }
  });

  it('has no disclosure element to hide anything behind', () => {
    const { container } = render(
      <ChatErrorPage error={toClientError(502)} onReturn={() => {}} />,
    );
    expect(container.querySelector('details')).toBeNull();
    expect(container.querySelector('summary')).toBeNull();
    expect(container.querySelector('pre')).toBeNull();
  });

  it('cannot leak an upstream payload even if one is forced into the props', () => {
    // A ClientError has no field for this, so the only way in is to lie to
    // the type system — which is exactly what a future regression would do.
    const hostile = {
      ...toClientError(502, 'MODEL_UNAVAILABLE'),
      detail: "Error code: 500 - {'error': {'message': 'connect ECONNREFUSED 10.0.0.4:8080'}}",
      requestId: 'req-abc-123',
      upstream: 'http://vllm:30000/v1/chat',
    } as unknown as ReturnType<typeof toClientError>;
    const { container } = render(
      <ChatErrorPage error={hostile} onReturn={() => {}} />,
    );
    const text = container.textContent ?? '';
    expect(text).not.toMatch(/ECONNREFUSED|10\.0\.0\.4|req-abc-123|vllm|30000/);
    expect(container.innerHTML).not.toMatch(/ECONNREFUSED|req-abc-123|vllm/);
  });
});
