/**
 * The product name's fallback, shared by the server page that reads the
 * runtime NEXT_PUBLIC_APP_NAME (app/page.tsx) and the client component that
 * receives it as a prop (components/ChatApp.tsx).
 *
 * A plain module on purpose: a server component that imports a constant from
 * a 'use client' file gets a client REFERENCE, not the string.
 */
export const DEFAULT_APP_NAME = 'TechSara AI';
