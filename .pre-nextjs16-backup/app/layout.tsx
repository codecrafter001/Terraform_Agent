import './globals.css';
import type { Metadata } from 'next';
import { Navbar } from '../components/Navbar';

export const metadata: Metadata = {
  title: 'TerraAgent — Safe AWS ClickOps to Terraform Multi-Agent System',
  description:
    'Stateful multi-agent system powered by LangGraph, FastAPI, and local LLMs to reverse-engineer AWS infrastructure into validated Terraform code with zero mutation.',
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="min-h-screen flex flex-col bg-white text-ink antialiased selection:bg-brand-200 selection:text-brand-900">
        <Navbar />
        <main className="flex-1">{children}</main>
      </body>
    </html>
  );
}
