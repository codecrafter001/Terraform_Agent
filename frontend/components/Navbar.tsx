import React from 'react';
import Image from 'next/image';
import Link from 'next/link';
import { ShieldCheck, Terminal, GitFork } from 'lucide-react';

export const Navbar: React.FC = () => {
  return (
    <nav className="border-b border-border bg-white/80 backdrop-blur-md sticky top-0 z-50">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="flex items-center justify-between h-16">
          <div className="flex items-center space-x-3">
            <Link
              href="/"
              className="flex items-center gap-3 py-1 px-2 rounded-xl hover:opacity-90 transition-opacity"
            >
              <Image
                src="/aikart-logo.jpeg"
                alt="AIKart"
                width={120}
                height={36}
                className="h-8 w-auto object-contain"
                priority
              />
              <span className="w-px h-5 bg-gray-200" />
              <span className="text-base font-bold text-ink whitespace-nowrap">
                Terra<span className="text-brand-600">Agent</span>
              </span>
            </Link>
            <span className="hidden sm:inline text-xs px-2 py-0.5 rounded-full border border-brand-200 text-brand-700 bg-brand-50 font-mono">
              v1.0 Ready
            </span>
          </div>

          <div className="flex items-center space-x-6 text-sm font-medium">
            <Link href="/scan" className="text-gray-600 hover:text-brand-600 transition-colors flex items-center space-x-1.5">
              <Terminal className="w-4 h-4" />
              <span>Launch Scan</span>
            </Link>
            <a
              href="https://github.com"
              target="_blank"
              rel="noopener noreferrer"
              className="text-gray-500 hover:text-ink transition-colors flex items-center space-x-1.5"
            >
              <GitFork className="w-4 h-4" />
              <span>Docs</span>
            </a>
            <div className="flex items-center space-x-1.5 text-xs text-gray-600 bg-surface px-3 py-1.5 rounded-full border border-border">
              <ShieldCheck className="w-4 h-4 text-brand-600" />
              <span>Read-Only Guardrails Enforced</span>
            </div>
          </div>
        </div>
      </div>
    </nav>
  );
};
