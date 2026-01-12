--
-- PostgreSQL database dump
--

\restrict vdhxbLf8gWEI9PybE6JKlRIptnYYRvnlhD3tYdUzP3Rc3xueFQQl6KdB9KSCNBv

-- Dumped from database version 18.0
-- Dumped by pg_dump version 18.1

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: accounts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.accounts (
    biz text NOT NULL,
    nickname text NOT NULL,
    alias text,
    round_head_img text,
    uin text NOT NULL,
    key text NOT NULL,
    pass_ticket text NOT NULL,
    is_default boolean DEFAULT false NOT NULL,
    last_synced_at timestamp with time zone,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: article_content; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.article_content (
    id integer NOT NULL,
    article_pk integer NOT NULL,
    url_token text,
    clean_html text,
    content_markdown text,
    content_json jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: article_content_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.article_content_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: article_content_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.article_content_id_seq OWNED BY public.article_content.id;


--
-- Name: article_images; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.article_images (
    id integer NOT NULL,
    article_pk integer NOT NULL,
    "position" integer NOT NULL,
    kind text NOT NULL,
    orig_url text,
    content_type text,
    data bytea,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: article_images_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.article_images_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: article_images_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.article_images_id_seq OWNED BY public.article_images.id;


--
-- Name: articles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.articles (
    id integer NOT NULL,
    biz text NOT NULL,
    article_id text NOT NULL,
    title text NOT NULL,
    author text,
    digest text,
    cover text,
    link text NOT NULL,
    source_url text,
    publish_at bigint,
    raw_json text NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: articles_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.articles_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: articles_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.articles_id_seq OWNED BY public.articles.id;


--
-- Name: login_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.login_sessions (
    id integer NOT NULL,
    token text NOT NULL,
    cookies_json text NOT NULL,
    nickname text,
    avatar text,
    is_default boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


--
-- Name: login_sessions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.login_sessions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: login_sessions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.login_sessions_id_seq OWNED BY public.login_sessions.id;


--
-- Name: meta; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.meta (
    key text NOT NULL,
    value text NOT NULL
);


--
-- Name: article_content id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.article_content ALTER COLUMN id SET DEFAULT nextval('public.article_content_id_seq'::regclass);


--
-- Name: article_images id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.article_images ALTER COLUMN id SET DEFAULT nextval('public.article_images_id_seq'::regclass);


--
-- Name: articles id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.articles ALTER COLUMN id SET DEFAULT nextval('public.articles_id_seq'::regclass);


--
-- Name: login_sessions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.login_sessions ALTER COLUMN id SET DEFAULT nextval('public.login_sessions_id_seq'::regclass);


--
-- Name: accounts accounts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.accounts
    ADD CONSTRAINT accounts_pkey PRIMARY KEY (biz);


--
-- Name: article_content article_content_article_pk_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.article_content
    ADD CONSTRAINT article_content_article_pk_key UNIQUE (article_pk);


--
-- Name: article_content article_content_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.article_content
    ADD CONSTRAINT article_content_pkey PRIMARY KEY (id);


--
-- Name: article_images article_images_article_pk_orig_url_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.article_images
    ADD CONSTRAINT article_images_article_pk_orig_url_key UNIQUE (article_pk, orig_url);


--
-- Name: article_images article_images_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.article_images
    ADD CONSTRAINT article_images_pkey PRIMARY KEY (id);


--
-- Name: articles articles_biz_article_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.articles
    ADD CONSTRAINT articles_biz_article_id_key UNIQUE (biz, article_id);


--
-- Name: articles articles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.articles
    ADD CONSTRAINT articles_pkey PRIMARY KEY (id);


--
-- Name: login_sessions login_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.login_sessions
    ADD CONSTRAINT login_sessions_pkey PRIMARY KEY (id);


--
-- Name: meta meta_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.meta
    ADD CONSTRAINT meta_pkey PRIMARY KEY (key);


--
-- Name: idx_articles_biz_publish; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_articles_biz_publish ON public.articles USING btree (biz, publish_at DESC);


--
-- Name: article_content article_content_article_pk_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.article_content
    ADD CONSTRAINT article_content_article_pk_fkey FOREIGN KEY (article_pk) REFERENCES public.articles(id) ON DELETE CASCADE;


--
-- Name: article_images article_images_article_pk_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.article_images
    ADD CONSTRAINT article_images_article_pk_fkey FOREIGN KEY (article_pk) REFERENCES public.articles(id) ON DELETE CASCADE;


--
-- Name: articles articles_biz_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.articles
    ADD CONSTRAINT articles_biz_fkey FOREIGN KEY (biz) REFERENCES public.accounts(biz) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict vdhxbLf8gWEI9PybE6JKlRIptnYYRvnlhD3tYdUzP3Rc3xueFQQl6KdB9KSCNBv
