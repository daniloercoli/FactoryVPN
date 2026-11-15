import type { components } from "./schema";

export type Enrollment = components["schemas"]["EnrollmentResponse"];
export type EnrollmentState = Enrollment["state"];
