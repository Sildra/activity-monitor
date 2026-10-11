import createClient from "openapi-fetch";
import type { paths } from "./generated/schema";

const baseUrl = ".";

export const api = createClient<paths>({ baseUrl });
