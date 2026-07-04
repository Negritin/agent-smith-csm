import { createClient, SupabaseClient } from '@supabase/supabase-js';

// Service Role Client for logging (bypasses RLS)
// Lazy initialization to avoid errors when imported on client-side
let _supabaseAdmin: SupabaseClient | null = null;

function getSupabaseAdmin(): SupabaseClient | null {
  // Only create on server-side where env vars exist
  if (typeof window !== 'undefined') {
    return null; // Running on client, can't use service role
  }

  if (!_supabaseAdmin && process.env.SUPABASE_SERVICE_ROLE_KEY) {
    _supabaseAdmin = createClient(
      process.env.NEXT_PUBLIC_SUPABASE_URL!,
      process.env.SUPABASE_SERVICE_ROLE_KEY!,
      { auth: { persistSession: false } },
    );
  }
  return _supabaseAdmin;
}

// =============================================
// ENVIRONMENT CHECK
// =============================================
const isDev = process.env.NODE_ENV !== 'production';

// =============================================
// TYPES - SYSTEM ACTION LOGGING
// =============================================
export type ActionType =
  | 'LOGIN_SUCCESS'
  | 'LOGIN_FAILED'
  | 'LOGOUT'
  | 'SIGNUP'
  | 'PASSWORD_RESET'
  | 'ADMIN_LOGIN'
  | 'ADMIN_LOGOUT'
  | 'USER_APPROVED'
  | 'USER_REJECTED'
  | 'USER_SUSPENDED'
  | 'USER_ACTIVATED'
  | 'USER_UPDATED'
  | 'COMPANY_CREATED'
  | 'COMPANY_UPDATED'
  | 'COMPANY_SUSPENDED'
  | 'COMPANY_ACTIVATED'
  | 'INVITE_GENERATED'
  | 'API_CALL'
  | 'N8N_WEBHOOK_CALL'
  | 'LANGCHAIN_API_CALL'
  | 'ERROR_OCCURRED'
  | 'SESSION_CREATED'
  | 'SESSION_EXPIRED'
  | 'SESSION_INVALID'
  | 'BACKEND_REQUEST'
  | 'BACKEND_ERROR';

export type LogStatus = 'success' | 'error' | 'warning';

export interface LogEntry {
  userId?: string;
  adminId?: string;
  companyId?: string;
  actionType: ActionType;
  resourceType?: string;
  resourceId?: string;
  details?: Record<string, any>;
  ipAddress?: string;
  userAgent?: string;
  sessionId?: string;
  status?: LogStatus;
  errorMessage?: string;
}

// =============================================
// SANITIZATION FUNCTIONS (SECURITY)
// =============================================

/**
 * Sanitize email for logging
 * usuario@exemplo.com -> usu***@exe***.com
 */
export function sanitizeEmail(email: string): string {
  if (!email || !email.includes('@')) return '***@***.***';

  const [local, domain] = email.split('@');
  const [domainName, domainExt] = domain.split('.');

  const sanitizedLocal = local.length > 3 ? local.substring(0, 3) + '***' : '***';

  const sanitizedDomain =
    domainName && domainName.length > 3 ? domainName.substring(0, 3) + '***' : '***';

  return `${sanitizedLocal}@${sanitizedDomain}.${domainExt || '***'}`;
}

/**
 * Sanitize IP address for logging
 * 192.168.1.100 -> 192.168.***.***
 */
export function sanitizeIP(ip: string): string {
  if (!ip) return '***';
  const parts = ip.split('.');
  if (parts.length === 4) {
    return `${parts[0]}.${parts[1]}.***.***`;
  }
  // IPv6 or other
  return ip.substring(0, 10) + '***';
}

// =============================================
// SECURE LOGGING (CONSOLE)
// =============================================

type LogLevel = 'info' | 'warn' | 'error' | 'debug';

interface LogOptions {
  email?: string;
  ip?: string;
  [key: string]: unknown;
}

/**
 * Secure logging function
 * In development: logs full data
 * In production: sanitizes sensitive data
 */
export function secureLog(level: LogLevel, message: string, options?: LogOptions): void {
  const timestamp = new Date().toISOString();

  let sanitizedOptions = options;

  if (!isDev && options) {
    sanitizedOptions = { ...options };

    if (options.email) {
      sanitizedOptions.email = sanitizeEmail(options.email);
    }
    if (options.ip) {
      sanitizedOptions.ip = sanitizeIP(options.ip);
    }

    // Remove any keys ending with Key, Secret, Token, Password
    for (const key of Object.keys(sanitizedOptions)) {
      if (/key|secret|token|password|hash/i.test(key)) {
        sanitizedOptions[key] = '[REDACTED]';
      }
    }
  }

  const logData = sanitizedOptions ? `${message} ${JSON.stringify(sanitizedOptions)}` : message;

  const prefix = `[${timestamp}]`;

  switch (level) {
    case 'error':
      console.error(prefix, logData);
      break;
    case 'warn':
      console.warn(prefix, logData);
      break;
    case 'debug':
      if (isDev) console.log(prefix, '[DEBUG]', logData);
      break;
    default:
      console.log(prefix, logData);
  }
}

// Convenience functions for secure logging
export const log = {
  info: (message: string, options?: LogOptions) => secureLog('info', message, options),
  warn: (message: string, options?: LogOptions) => secureLog('warn', message, options),
  error: (message: string, options?: LogOptions) => secureLog('error', message, options),
  debug: (message: string, options?: LogOptions) => secureLog('debug', message, options),
};

export function errorLogFields(error: unknown): LogOptions {
  if (error instanceof Error) {
    return {
      errorName: error.name,
    };
  }

  return {
    errorType: typeof error,
  };
}

// =============================================
// SYSTEM ACTION LOGGING (DATABASE)
// =============================================

/**
 * Log system action to database
 * Used for audit trail and security monitoring
 */
export async function logSystemAction(entry: LogEntry): Promise<void> {
  try {
    const supabase = getSupabaseAdmin();

    // Skip logging if running on client-side (no service role available)
    if (!supabase) {
      console.log('[LOGGER] Skipping log (client-side):', entry.actionType);
      return;
    }

    // Sanitize sensitive data in production before storing
    const sanitizedDetails =
      !isDev && entry.details ? sanitizeLogDetails(entry.details) : entry.details;

    const { error } = await supabase.from('system_logs').insert({
      timestamp: new Date().toISOString(),
      user_id: entry.userId || null,
      admin_id: entry.adminId || null,
      company_id: entry.companyId || null,
      action_type: entry.actionType,
      resource_type: entry.resourceType || null,
      resource_id: entry.resourceId || null,
      details: sanitizedDetails || {},
      ip_address: entry.ipAddress || null,
      user_agent: entry.userAgent || null,
      session_id: entry.sessionId || null,
      status: entry.status || 'success',
      error_message: entry.errorMessage || null,
    });

    if (error) {
      log.error('[LOGGER] Error inserting log', errorLogFields(error));
    }
  } catch (error) {
    log.error('[LOGGER] Failed to log action', errorLogFields(error));
  }
}

/**
 * Sanitize log details object
 * Removes sensitive data from details before storing
 */
function sanitizeLogDetails(details: Record<string, any>): Record<string, any> {
  const sanitized = { ...details };

  for (const key of Object.keys(sanitized)) {
    // Redact sensitive keys
    if (/key|secret|token|password|hash/i.test(key)) {
      sanitized[key] = '[REDACTED]';
    }
    // Sanitize email fields
    if (/email/i.test(key) && typeof sanitized[key] === 'string') {
      sanitized[key] = sanitizeEmail(sanitized[key]);
    }
  }

  return sanitized;
}

/**
 * Get client info from request
 * Extracts IP address and user agent for logging
 */
export function getClientInfo(request: Request): { ipAddress?: string; userAgent?: string } {
  const forwardedFor = request.headers.get('x-forwarded-for');
  const realIp = request.headers.get('x-real-ip');
  const ipAddress = forwardedFor?.split(',')[0].trim() || realIp || undefined;
  const userAgent = request.headers.get('user-agent') || undefined;

  return { ipAddress, userAgent };
}
