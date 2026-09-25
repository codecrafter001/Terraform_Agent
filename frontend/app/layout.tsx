import './globals.css';
import type { Metadata } from 'next';
import { Sidebar } from '../components/Sidebar';

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
      <body className="min-h-screen bg-surface text-ink antialiased selection:bg-brand-200 selection:text-brand-900">
        <Sidebar />
        <div className="lg:pl-64 min-h-screen flex flex-col">
          <main className="flex-1 w-full max-w-7xl mx-auto px-4 sm:px-6 lg:px-10 py-6 sm:py-8">{children}</main>
        </div>
      </body>
    </html>
  );
}
