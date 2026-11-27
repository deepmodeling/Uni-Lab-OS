-- 兼容函数只处理已有 SQL 的取值规则，不引入业务数据或独立 schema。
CREATE OR REPLACE FUNCTION public.ulab_json_valid(value text) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    IF value IS NULL THEN RETURN false; END IF;
    PERFORM value::jsonb;
    RETURN true;
EXCEPTION WHEN invalid_text_representation THEN RETURN false;
END $$;

CREATE OR REPLACE FUNCTION public.ulab_json_path(path text) RETURNS text[]
LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT CASE WHEN path = '$' THEN ARRAY[]::text[]
        ELSE string_to_array(trim(both '.' from regexp_replace(substr(path, 2), '\[([0-9]+)\]', '.\1', 'g')), '.') END
$$;

CREATE OR REPLACE FUNCTION public.ulab_json_get(value text, path text) RETURNS jsonb
LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT value::jsonb #> public.ulab_json_path(path) $$;

CREATE OR REPLACE FUNCTION public.ulab_json_scalar(value text, path text) RETURNS jsonb
LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT CASE public.ulab_json_get(value,path) WHEN 'true'::jsonb THEN '1'::jsonb
        WHEN 'false'::jsonb THEN '0'::jsonb ELSE public.ulab_json_get(value,path) END
$$;

CREATE OR REPLACE FUNCTION public.ulab_json_extract(value text, path text) RETURNS text
LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT CASE public.ulab_json_get(value,path) WHEN 'true'::jsonb THEN '1' WHEN 'false'::jsonb THEN '0' ELSE value::jsonb #>> public.ulab_json_path(path) END $$;

CREATE OR REPLACE FUNCTION public.ulab_json_type(value text, path text DEFAULT '$') RETURNS text
LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT CASE jsonb_typeof(public.ulab_json_get(value, path))
        WHEN 'string' THEN 'text'
        WHEN 'boolean' THEN public.ulab_json_extract(value, path)
        WHEN 'number' THEN CASE WHEN public.ulab_json_extract(value, path) ~ '^-?[0-9]+$' THEN 'integer' ELSE 'real' END
        ELSE jsonb_typeof(public.ulab_json_get(value, path)) END
$$;

CREATE OR REPLACE FUNCTION public.ulab_json_each(document text, path text DEFAULT '$')
RETURNS TABLE(key text, value text) LANGUAGE plpgsql IMMUTABLE STRICT AS $$
DECLARE selected jsonb := public.ulab_json_get(document, path);
BEGIN
    IF jsonb_typeof(selected) = 'array' THEN
        RETURN QUERY SELECT (ordinality - 1)::text, CASE WHEN jsonb_typeof(e) = 'string' THEN e #>> '{}' ELSE e::text END
            FROM jsonb_array_elements(selected) WITH ORDINALITY AS a(e, ordinality);
    ELSIF jsonb_typeof(selected) = 'object' THEN
        RETURN QUERY SELECT k, CASE WHEN jsonb_typeof(v) = 'string' THEN v #>> '{}' ELSE v::text END FROM jsonb_each(selected) AS a(k,v);
    END IF;
END $$;

CREATE OR REPLACE FUNCTION public.ulab_json_set(document text, path text, value jsonb) RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE result jsonb := document::jsonb; keys text[] := public.ulab_json_path(path); i integer;
BEGIN
    FOR i IN 1..coalesce(array_length(keys,1),0)-1 LOOP
        IF result #> keys[1:i] IS NULL THEN result := jsonb_set(result, keys[1:i], '{}'::jsonb, true); END IF;
    END LOOP;
    RETURN jsonb_set(result, keys, coalesce(value,'null'::jsonb), true)::text;
END $$;

CREATE OR REPLACE FUNCTION public.ulab_strftime(format text, value text) RETURNS text
LANGUAGE plpgsql STABLE STRICT AS $$
DECLARE instant timestamp := CASE WHEN value = 'now' THEN CURRENT_TIMESTAMP AT TIME ZONE 'UTC' ELSE value::timestamp END;
BEGIN
    IF format = '%Y-%m-%dT%H:%M:%fZ' THEN RETURN to_char(instant, 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'); END IF;
    IF format = '%Y-%m-%d %H:%M:%f' THEN RETURN to_char(instant, 'YYYY-MM-DD HH24:MI:SS.MS'); END IF;
    IF format = '%Y-%m-%d %H:%M:%S' THEN RETURN to_char(instant, 'YYYY-MM-DD HH24:MI:SS'); END IF;
    IF format = '%s' THEN RETURN floor(extract(epoch FROM instant AT TIME ZONE 'UTC'))::bigint::text; END IF;
    RAISE EXCEPTION '不支持的时间格式：%', format;
END $$;

CREATE OR REPLACE FUNCTION public.ulab_glob(value text, pattern text) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE STRICT AS $$
DECLARE result text := '^'; c text; i integer := 1; in_class boolean := false;
BEGIN
    WHILE i <= length(pattern) LOOP
        c := substr(pattern, i, 1);
        IF c = '[' THEN in_class := true; result := result || c;
        ELSIF c = ']' THEN in_class := false; result := result || c;
        ELSIF in_class THEN result := result || c;
        ELSIF c = '*' THEN result := result || '.*';
        ELSIF c = '?' THEN result := result || '.';
        ELSIF strpos('.+(){}^$|\', c) > 0 THEN result := result || '\' || c;
        ELSE result := result || c;
        END IF;
        i := i + 1;
    END LOOP;
    RETURN value ~ (result || '$');
END $$;

CREATE OR REPLACE FUNCTION public.ulab_julianday(value text) RETURNS double precision
LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT extract(epoch FROM value::timestamptz)::double precision / 86400.0 + 2440587.5
$$;
