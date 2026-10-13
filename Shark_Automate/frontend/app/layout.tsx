import type { Metadata } from "next";
import "./globals.css";
import { Nav } from "@/components/Nav";

export const metadata: Metadata = {
  title: "TV → Shark Trading Terminal",
  description: "Professional TradingView → Shark Exchange automated trading platform",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen">
        <div className="flex min-h-screen">
          <Nav />
          <main className="flex-1 min-w-0 p-4 lg:p-5 space-y-4">{children}</main>
        </div>
      </body>
    </html>
  );
}
