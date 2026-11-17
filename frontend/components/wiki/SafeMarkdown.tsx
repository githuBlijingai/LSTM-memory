"use client";

import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";

/**
 * 设计 8.4：仅渲染经过白名单清洗的 Markdown，禁止 Raw HTML、脚本、iframe 与内联事件。
 * 外链统一附加安全属性。
 */
export function SafeMarkdown({ children }: { children: string }) {
  return (
    <div className="markdown-body">
      <ReactMarkdown
        components={{
          a: ({ href, children: linkChildren }) => (
            <a href={href} rel="noopener noreferrer" target="_blank">
              {linkChildren}
            </a>
          ),
        }}
        rehypePlugins={[rehypeSanitize]}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
