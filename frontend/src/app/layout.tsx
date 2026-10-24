import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "QuantAgent Lab｜量化研究工作台",
  description: "可追溯的多代理量化研究、歷史回測與決策分析。",
};
export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-Hant">
      <body>{children}</body>
    </html>
  );
}
