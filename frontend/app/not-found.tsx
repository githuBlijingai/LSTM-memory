import { Button, Result } from "antd";
import Link from "next/link";

export default function NotFound() {
  return (
    <Result
      extra={<Link href="/dashboard"><Button type="primary">返回概览</Button></Link>}
      status="404"
      subTitle="访问的页面不存在或已移动。"
      title="页面不存在"
    />
  );
}
