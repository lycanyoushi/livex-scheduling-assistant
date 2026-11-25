import { ArrowUpRight } from "lucide-react";

import { OUTLINE_BUTTON } from "./chat-cards";

export function BookerFallback() {
  return (
    <a href="https://app.cal.com/bookings/upcoming" target="_blank" rel="noopener noreferrer"
      className={`${OUTLINE_BUTTON} mt-2 inline-flex items-center gap-1.5 border border-border bg-card`}>
      Open Cal.com bookings
      <ArrowUpRight size={15} aria-hidden="true" />
    </a>
  );
}
