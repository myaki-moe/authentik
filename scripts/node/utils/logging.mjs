/**
 * @file Lightweight logging utilities for Node.js scripts.
 */

/**
 * @typedef {Object} Logger
 * @property {typeof console.info} info
 * @property {typeof console.warn} warn
 * @property {typeof console.error} error
 * @property {typeof console.debug} debug
 */

/**
 * Creates a logger with the given prefix for all messages.
 * @param {string} prefix
 * @return {Logger}
 */
export function createLogger(prefix) {
    prefix = `[${prefix}]`;

    const logger = {
        info: console.info.bind(console, "INFO", prefix),
        error: console.error.bind(console, "ERROR", prefix),
        warn: console.warn.bind(console, "WARN", prefix),
        debug: console.debug.bind(console, "DEBUG", prefix),
    };

    return logger;
}
