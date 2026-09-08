#!/usr/bin/env node
/**
 * TypeScript-protobus service fixture for the cross-language test
 * (tests/integration/test_cross_language.py): Python client -> TS server.
 *
 * Serves streaming_test.Counter from ../../streaming_proto/streaming_test.proto
 * plus a Wallet service exercising custom types and events, using the BUILT
 * TypeScript checkout at PROTOBUS_TS (default ../protobus next to this repo).
 * Prints READY on stdout once consuming; exits on SIGTERM.
 */
const path = require('path');
const fs = require('fs');

const TS = process.env.PROTOBUS_TS || path.resolve(__dirname, '../../../../protobus');
const AMQP = process.env.PROTOBUS_TEST_AMQP || 'amqp://guest:guest@localhost:5672/';
const PROTO_DIR = process.env.PROTOBUS_TEST_PROTO_DIR || path.resolve(__dirname, '../../streaming_proto');
const SUFFIX = process.env.PROTOBUS_TEST_SUFFIX || '';

const Context = require(path.join(TS, 'dist/lib/context')).default;
const MessageService = require(path.join(TS, 'dist/lib/message_service')).default;
const { HandledError } = require(path.join(TS, 'dist/lib/errors'));
const { setLevel, LogLevel } = require(path.join(TS, 'dist/lib/logger'));
setLevel(LogLevel.Warn);

const WALLET_PROTO = `syntax = "proto3";
package Wallet${SUFFIX};
message Balance { bigint amount = 1; timestamp as_of = 2; int64 big = 3; repeated string tags = 4; map<string, int32> counts = 5; }
message Query { string account = 1; int32 zero = 2; }
message Ping { string id = 1; bigint n = 2; }
message Produced { int32 yielded = 1; bool stopped_early = 2; bool finished = 3; }
message Nothing {}
service Api { rpc balance (Query) returns (Balance); rpc produced (Nothing) returns (Produced); }`;

// What the streaming producer actually did, so the Python side can assert a
// cancellation reached it rather than only that the client stopped reading.
const produced = { yielded: 0, stopped_early: false, finished: false };

class Counter extends MessageService {
    get ServiceName() { return 'streaming_test.Counter'; }
    get ProtoFileName() { return path.join(PROTO_DIR, 'streaming_test.proto'); }
    async add(req) { return { sum: (req.a || 0) + (req.b || 0) }; }
    async *tick(req, _actor, _id, context) {
        if (req.emit_nothing) return;
        produced.yielded = 0; produced.stopped_early = false; produced.finished = false;
        for (let i = 0; i < (req.count || 0); i++) {
            if (req.fail_at && i >= req.fail_at) throw new HandledError(`deliberate failure at chunk ${i}`, 'TEST_FAIL');
            if (context && context.signal && context.signal.aborted) { produced.stopped_early = true; return; }
            produced.yielded += 1;
            yield { seq: i, payload: `chunk-${i}` };
            if (req.delay_ms) await new Promise(r => setTimeout(r, req.delay_ms));
        }
        produced.finished = true;
    }
}

class Wallet extends MessageService {
    constructor(context) { super(context, { retry: { maxRetries: 0 } }); }
    get ServiceName() { return `Wallet${SUFFIX}.Api`; }
    get ProtoFileName() { return ''; }
    get Proto() { return WALLET_PROTO; }
    async produced() { return { ...produced }; }
    async balance(req) {
        if (req.account === 'boom') throw new HandledError('no such account', 'NOT_FOUND');
        return {
            amount: 10n ** 30n + BigInt(req.zero),
            as_of: new Date(Date.UTC(2020, 0, 1)),
            big: '9007199254740993',
            tags: ['a', 'b'],
            counts: { x: 1, y: 2 },
        };
    }
}

(async () => {
    const ctx = new Context();
    await ctx.init(AMQP, [PROTO_DIR]);
    const counter = new Counter(ctx);
    await counter.init();
    const wallet = new Wallet(ctx);
    await wallet.init();
    // Echo every Ping event back as a Pong-topic event, so the Python side
    // can see TS both received its event and emitted one.
    await wallet.subscribeEvent(`Wallet${SUFFIX}.Ping`, async (event, type, topic) => {
        await ctx.publishEvent(`Wallet${SUFFIX}.Ping`, { id: `pong:${event.id}`, n: event.n + 1n }, `EVENT.pong${SUFFIX}`);
    }, `EVENT.ping${SUFFIX}`);
    process.stdout.write('READY\n');
    const stop = () => { ctx.connection.disconnect().finally(() => process.exit(0)); };
    process.on('SIGTERM', stop);
    process.on('SIGINT', stop);
})().catch((err) => { console.error(err); process.exit(1); });
