import type { ReactNode } from "react";

export function PageHeader({
  title,
  description,
  extra,
}: {
  title: string;
  description?: ReactNode;
  extra?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div className="page-header-main">
        <h1>{title}</h1>
        {description ? <p>{description}</p> : null}
      </div>
      {extra ? <div className="page-header-extra">{extra}</div> : null}
    </header>
  );
}
