import { ChatApp } from '@/components/ChatApp';
import { DEFAULT_APP_NAME } from '@/lib/appName';

/**
 * The chat home. A server component on purpose: it reads the product name
 * from the RUNTIME environment and hands it to ChatApp as a prop, so the
 * server HTML and the client's hydration render the same text. ChatApp must
 * not read NEXT_PUBLIC_APP_NAME itself (see the note above its h1 prop).
 */
export default function ChatPage() {
  return <ChatApp appName={process.env.NEXT_PUBLIC_APP_NAME ?? DEFAULT_APP_NAME} />;
}
